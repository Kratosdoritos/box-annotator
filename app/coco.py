"""
COCO annotation file helpers.

Backs the annotator UI: draw labelled boxes on dataset images one by one and
export them as a standard COCO-format JSON file (images / annotations /
categories) suitable for training a detector. The classes live in the file's
"categories" list, so each dataset carries its own.

  Images live in:        <dataset>/images/
  Annotations saved to:  <dataset>/coco_annotations.json

The parsed file stays in memory until it changes on disk, so requests don't
re-read a big file every time. Every access goes through LOCK, and changes go
through `edit()`, so two requests never interleave their changes.
"""

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from .config import DATASET_DIR, logger

ANNOT_IMAGES_DIR = DATASET_DIR / "images"                # images to annotate
COCO_PATH = DATASET_DIR / "coco_annotations.json"        # the exported COCO file
ANNOT_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}           # types a browser <img> can display (no TIFF)
# fields the app relies on; a file missing them is treated as corrupt rather than crashing every request
REQUIRED = {"images": ("id", "file_name"), "annotations": ("id", "image_id", "category_id", "bbox"),
            "categories": ("id", "name")}

RETRY_DELAY = 0.05         # s; the wait grows with each retry of a file another program holds
LOCK = threading.RLock()   # held while reading or changing the COCO data
_cache: dict = {}          # {"key": file identity, "data": parsed file, "sha": its sha256 or None}
_encode = json.JSONEncoder(ensure_ascii=False).encode


def empty_coco() -> dict:
    """A fresh COCO skeleton with no classes yet (they're added from the UI)."""
    return {"images": [], "annotations": [], "categories": []}


def next_id(records: list[dict]) -> int:
    """The id after the largest one in a COCO section (1 for an empty one)."""
    return max((r["id"] for r in records), default=0) + 1


def clip_box(x: float, y: float, w: float, h: float, width: int, height: int) -> list[float] | None:
    """
    Clip a box to the image and round it to 2 decimals.

    Returns:
        The COCO [x, y, w, h] box, or None if no part of it is inside the image.
    """
    x1, y1 = min(max(x, 0), width), min(max(y, 0), height)
    x2, y2 = min(max(x + w, 0), width), min(max(y + h, 0), height)
    x, y, w, h = round(x1, 2), round(y1, 2), round(x2 - x1, 2), round(y2 - y1, 2)
    return [x, y, w, h] if w > 0 and h > 0 else None


def natural_key(name: str) -> tuple:
    """Sort key that puts img2 before img10 and ignores case."""
    # split() puts the digit runs at the odd positions
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"(\d+)", name))], name


def list_dataset_images() -> list[str]:
    """
    Return the file names of every annotatable image in the dataset dir, in natural order.

    Returns:
        A list of bare file names (not paths); empty if the folder is missing.
    """
    try:
        # scandir knows which entries are files without a stat call per file: fast for big folders
        with os.scandir(ANNOT_IMAGES_DIR) as entries:
            names = [e.name for e in entries if os.path.splitext(e.name)[1].lower() in ANNOT_EXTS and e.is_file()]
    except FileNotFoundError:
        return []
    return sorted(names, key=natural_key)


def retry(action, attempts: int = 6):
    """Run a file operation, retrying briefly while another program (e.g. a virus scanner) holds the file."""
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))


def _file_key():
    """Identifies the file's current version, or None if it doesn't exist."""
    try:
        st = os.stat(COCO_PATH)
    except FileNotFoundError:
        return None
    return str(COCO_PATH), st.st_ino, st.st_mtime_ns, st.st_size


def _well_typed(section: str, r: dict) -> bool:
    """The fields the app computes with have the types COCO gives them (ids are whole numbers)."""
    if type(r["id"]) is not int:   # not bool either
        return False
    if section == "annotations":
        bbox = r["bbox"]
        return (type(r["image_id"]) is int and type(r["category_id"]) is int and isinstance(bbox, list)
                and len(bbox) == 4 and all(type(v) in (int, float) for v in bbox))
    return isinstance(r["file_name" if section == "images" else "name"], str)


def _parse(raw: bytes) -> dict:
    """Parse and check the COCO file; ValueError if it isn't one the app can use."""
    data = json.loads(raw.decode("utf-8-sig"))   # -sig: a byte-order mark from a Windows editor is fine
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    for section, fields in REQUIRED.items():
        records = data.setdefault(section, [])   # backfill any section an older/partial file is missing
        if not isinstance(records, list):
            raise ValueError(f'"{section}" is not a list')
        for r in records:
            if not isinstance(r, dict) or any(f not in r for f in fields):
                raise ValueError(f'a record in "{section}" lacks one of {", ".join(fields)}')
            if not _well_typed(section, r):
                raise ValueError(f'a record in "{section}" has a field of the wrong type: {r}')
    # a box pointing at a class the file doesn't list would be stuck
    # (it could never be re-saved), so give that id a placeholder name
    known = {c["id"] for c in data["categories"]}
    for cid in sorted({a["category_id"] for a in data["annotations"]} - known):
        data["categories"].append({"id": cid, "name": f"class {cid}", "supercategory": ""})
    return data


def load_coco() -> dict:
    """
    Load the COCO file, or a fresh skeleton if it's missing or corrupt.

    Always returns a dict with "images", "annotations" and "categories" keys
    present, so callers never have to guard against missing sections.
    The result is shared with other requests: read it while holding LOCK,
    and change it only inside `edit()`.

    Returns:
        The COCO data dict.
    """
    with LOCK:
        key = _file_key()
        if key is None:
            return empty_coco()
        if _cache.get("key") == key:
            return _cache["data"]
        raw = retry(COCO_PATH.read_bytes)    # a read error is not corruption: let it raise
        try:
            data = _parse(raw)
        except ValueError as e:
            # unreadable/corrupt file -> move it aside so the next save can't
            # silently overwrite it, then start clean rather than crash
            stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
            backup = COCO_PATH.with_name(f"{COCO_PATH.stem}.corrupt-{stamp}.json")
            while backup.exists():   # never replace an earlier backup
                n += 1
                backup = COCO_PATH.with_name(f"{COCO_PATH.stem}.corrupt-{stamp}-{n}.json")
            retry(lambda: COCO_PATH.replace(backup))
            _cache.clear()
            logger.error(f"Could not read COCO file ({e}); moved it to {backup.name}, starting fresh")
            return empty_coco()
        _cache.update(key=key, data=data, sha=None)
        return data


def set_aside() -> list[str]:
    """Labels files moved aside as unreadable, oldest first: the user's labels, until they fix or delete them."""
    return [str(p) for p in sorted(DATASET_DIR.glob(f"{COCO_PATH.stem}.corrupt-*.json"))]


def dumps(data: dict) -> str:
    """JSON with one record per line: still readable, and several times faster to write than indent=2."""
    parts = []
    for key, value in data.items():
        if isinstance(value, list) and value:
            parts.append(f"{_encode(key)}: [\n" + ",\n".join(map(_encode, value)) + "\n]")
        else:
            parts.append(f"{_encode(key)}: {_encode(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def save_coco(data: dict) -> None:
    """
    Write the COCO data dict back to disk (UTF-8, one record per line).

    Writes to a temp file first and swaps it in, so a crash mid-write can't
    leave a truncated COCO file behind.

    Args:
        data: the full COCO dict to persist. Creates the dataset dir if needed.
    """
    with LOCK:
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        tmp = COCO_PATH.with_name(COCO_PATH.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(dumps(data).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())               # on disk before it replaces the old file
        retry(lambda: os.replace(tmp, COCO_PATH))
        _cache.update(key=_file_key(), data=data, sha=None)


@contextmanager
def edit() -> Iterator[dict]:
    """
    Load the COCO data to change it. It's saved when the block ends, and not if the block raises.
    Refuse a request (HTTPException) before changing anything, so the copy in memory stays good.
    """
    with LOCK:
        data = load_coco()
        try:
            yield data
            save_coco(data)
        except HTTPException:
            raise   # refused before any change: keep the copy, or a big file is re-read after every 409
        except BaseException:
            # the shared copy may be half-changed, or changed but not saved: read the file again next time
            _cache.clear()
            raise


def snapshot() -> dict:
    """The COCO data as it is now, for slow work done without holding LOCK. Don't change the records."""
    with LOCK:
        return {k: list(v) if isinstance(v, list) else v for k, v in load_coco().items()}


def fingerprint() -> str:
    """sha256 of the saved COCO file ("" if there isn't one), to tell whether it changed since an export."""
    with LOCK:
        load_coco()                            # brings the cache in line with the file
        if _file_key() is None:
            return ""
        if _cache["sha"] is None:
            _cache["sha"] = hashlib.sha256(retry(COCO_PATH.read_bytes)).hexdigest()
        return _cache["sha"]
