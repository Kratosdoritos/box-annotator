"""
Workspace endpoints: get images in, get datasets out, free the space afterwards.

  GET   /api/workspace                  where the workspace is, what's in it, whether it can be cleared
  PUT   /api/images/{file_name}         import one image (raw bytes in the body); never overwrites
  POST  /api/import/coco                merge labels from a COCO file
  GET   /api/export/{format}            download an export (.json, or .zip with images)
  POST  /api/export/{format}/download   prepare an export; the browser then downloads it from the url returned
  GET   /api/downloads/{token}/{name}   a prepared export (deleted once sent)
  POST  /api/export/{format}            write an export into a folder on this computer
  POST  /api/pick-folder                open this computer's folder picker
  POST  /api/workspace/clear            delete the imported images and their labels

Imports are copies, so clearing the workspace never touches the user's
originals. The folder endpoints and Clear only answer requests from this
computer: a browser elsewhere on the network has no business writing to its
disk or deleting the workspace.
"""

import errno
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import coco, config, exports, yolo
from .config import logger
from .exports import Files
from .schemas import ClearRequest, CocoImport, ExportFormat, ExportOptions, ExportRequest, PickFolderRequest

router = APIRouter()

MAX_IMAGE_BYTES = 50 * 1024 * 1024
# first bytes of each supported type, so a renamed text file can't pose as an image
MAGIC = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".bmp": b"BM"}
LOCAL_HOSTS = {"127.0.0.1", "::1"}
EXPORT_MARK = ".last_export"   # fingerprint of the labels as last exported
# prepared exports waiting for the browser; one folder per user, since the temp folder is shared on Linux and macOS
DOWNLOADS = Path(tempfile.gettempdir()) / ("box-annotator-downloads" + (f"-{os.getuid()}" if os.name == "posix" else ""))
TOKEN = re.compile(r"[0-9a-f]{16}")
CHUNK = 1024 * 1024
# errors that say the disk can't take the file, rather than that its name is no good
DISK_ERRORS = {errno.ENOSPC, errno.EROFS, errno.EIO, getattr(errno, "EDQUOT", errno.ENOSPC)}

# run in a child process: GUI toolkits want the main thread, and a crash there can't take the server down
PICKER = (
    "import sys, tkinter\n"
    "from tkinter import filedialog\n"
    "sys.stdout.reconfigure(encoding='utf-8')\n"
    "root = tkinter.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
    "print(filedialog.askdirectory(parent=root, title=sys.argv[1], initialdir=sys.argv[2] or None) or '')\n"
)
_picker_open = threading.Lock()
_store_lock = threading.Lock()
_exports_running = 0   # changed under coco.LOCK; the workspace isn't cleared while an export reads it


@contextmanager
def _exporting() -> Iterator[None]:
    global _exports_running
    with coco.LOCK:
        _exports_running += 1
    try:
        yield
    finally:
        with coco.LOCK:
            _exports_running -= 1


def _write_failed(e: OSError, what: str = "the export") -> HTTPException:
    """A file that couldn't be written, e.g. on a full disk: say why rather than a bare 500."""
    return HTTPException(status_code=500, detail=f"Couldn't write {what}: {e.strerror or e}")


def _is_local(request: Request) -> bool:
    return request.client is not None and request.client.host in LOCAL_HOSTS


def _local_only(request: Request, refusal: str = "Saving to a folder only works on the computer running the app; use Download") -> None:
    if not _is_local(request):
        raise HTTPException(status_code=403, detail=refusal)


def _exported() -> bool:
    mark = coco.DATASET_DIR / EXPORT_MARK
    return mark.exists() and mark.read_text(encoding="utf-8") == coco.fingerprint()


def _mark_exported(fingerprint: str) -> None:
    """Remember the labels an export was made from; a later change means it's out of date."""
    (coco.DATASET_DIR / EXPORT_MARK).write_text(fingerprint, encoding="utf-8")


def _unexported(set_aside: list[str]) -> bool:
    """Whether a Clear would lose labels: some changed since the last export, or an unreadable file was set aside."""
    return bool(set_aside) or (bool(coco.load_coco()["images"]) and not _exported())


@router.get("/api/workspace")
def workspace_status(request: Request):
    _sweep_downloads()   # the UI asks on every page load: old downloads go even if no new one is made
    images = len(coco.list_dataset_images())
    with coco.LOCK:
        labelled = len(coco.load_coco()["images"])
        set_aside = coco.set_aside()
        exported = not _unexported(set_aside)
    return {
        "images_dir": str(coco.ANNOT_IMAGES_DIR),
        "images": images,
        "labelled": labelled,
        "exported": exported,
        "set_aside": set_aside,   # the UI tells the user, who would otherwise only see their labels gone
        "can_clear": config.MANAGED_WORKSPACE and _is_local(request),
        "local": _is_local(request),
    }


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _same_file(path: Path, data: bytes) -> bool | None:
    """None if nothing is at `path`, else whether it holds exactly these bytes."""
    size = _size(path)
    if size is None:
        return None
    return size == len(data) and path.read_bytes() == data


def _store_image(path: Path, data: bytes) -> bool:
    """
    Write an imported image under a temp name, then move it in place. False if
    the very same file is already there; FileExistsError if a different one is.
    """
    same = _same_file(path, data)
    if same is None:
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.part")
        try:
            tmp.write_bytes(data)
            with _store_lock:   # the name may have been taken meanwhile
                same = _same_file(path, data)
                if same is None:
                    os.replace(tmp, path)
                    return True
        finally:
            tmp.unlink(missing_ok=True)
    if same:
        return False
    raise FileExistsError(path.name)


def _claim_labels(name: str) -> None:
    """Labels imported before their image under other capitals (IMG1.PNG for img1.png) now belong to it."""
    lower = name.lower()
    with coco.LOCK:
        images = coco.load_coco()["images"]
        if (not any(i["file_name"].lower() == lower and i["file_name"] != name for i in images)
                or any(i["file_name"] == name for i in images)):
            return   # the usual case
        on_disk = set(coco.list_dataset_images())
        with coco.edit() as data:
            waiting = next((i for i in data["images"] if i["file_name"].lower() == lower and i["file_name"] not in on_disk), None)
            if waiting:
                waiting["file_name"] = name


@router.put("/api/images/{file_name}", status_code=201)
async def import_image(file_name: str, request: Request):
    """
    Copy one image into the workspace. An existing image is never replaced,
    because its saved labels belong to that picture. Sending the very same
    file again is fine: 200 with "existing": true.
    """
    if Path(file_name).name != file_name or "\\" in file_name:
        raise HTTPException(status_code=400, detail="Send a plain file name, not a path")
    ext = Path(file_name).suffix.lower()
    if ext not in coco.ANNOT_EXTS:
        raise HTTPException(status_code=415, detail="Only .jpg, .jpeg, .png and .bmp images can be imported")
    too_big = HTTPException(status_code=413, detail="Images over 50 MB can't be imported")
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_IMAGE_BYTES:
        raise too_big
    path = coco.ANNOT_IMAGES_DIR / file_name
    taken = HTTPException(status_code=409, detail=f"A different image called {file_name} is already in the workspace")
    if length and _size(path) not in (None, length):
        raise taken   # can't be the same file: no need to read it
    # read up to the limit and no further: a body sent without a length could be any size
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise too_big
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(MAGIC[ext]):
        raise HTTPException(status_code=422, detail=f"{file_name} isn't a valid {ext} image")

    coco.ANNOT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        added = await run_in_threadpool(_store_image, path, data)   # off the event loop: images can be big
    except FileExistsError:
        raise taken from None
    except OSError as e:
        if isinstance(e, PermissionError) or e.errno in DISK_ERRORS:   # e.g. a full disk: not the name's fault
            raise _write_failed(e, f"{file_name} into the workspace") from e
        raise HTTPException(status_code=400, detail=f"Can't save a file called {file_name} here ({e.strerror or e})") from e
    if not added:
        return JSONResponse({"file_name": file_name, "existing": True})
    await run_in_threadpool(_claim_labels, file_name)
    return {"file_name": file_name}


@router.post("/api/import/coco")
def import_coco(body: CocoImport):
    """
    Merge labels from a COCO file. Classes are matched by name. Images that
    already have saved labels in the workspace are skipped, so an import never
    replaces your work. Labels for images not imported yet are kept and show
    up once the images arrive.
    """
    seen = set()
    for img in body.images:
        if img.id in seen:
            raise HTTPException(status_code=422, detail=f"The labels file lists image id {img.id} more than once")
        seen.add(img.id)
    present = set(coco.list_dataset_images())
    by_lower = {n.lower(): n for n in present}   # labels saying IMG1.JPG belong to img1.jpg on disk

    with coco.edit() as coco_data:
        by_name = {c["name"].lower(): c["id"] for c in coco_data["categories"]}
        names = {c.id: c.name.strip() or f"class {c.id}" for c in body.categories}
        for a in body.annotations:   # a box pointing at an unlisted class gets a placeholder, as on load
            names.setdefault(a.category_id, f"class {a.category_id}")
        cat_map, classes_added = {}, 0
        for src_id, name in names.items():
            if name.lower() not in by_name:
                by_name[name.lower()] = coco.next_id(coco_data["categories"])
                coco_data["categories"].append({"id": by_name[name.lower()], "name": name, "supercategory": ""})
                classes_added += 1
            cat_map[src_id] = by_name[name.lower()]

        taken = {img["file_name"] for img in coco_data["images"]}
        img_map, skipped, missing = {}, 0, 0
        next_img = coco.next_id(coco_data["images"])
        for img in body.images:
            name = img.file_name.replace("\\", "/").rsplit("/", 1)[-1]   # some tools store paths
            if name not in present:
                name = by_lower.get(name.lower(), name)
            if name in taken:
                skipped += 1
                continue
            record = {"id": next_img, "file_name": name, "width": img.width, "height": img.height}
            coco_data["images"].append(record)
            img_map[img.id] = record
            taken.add(name)
            missing += name not in present
            next_img += 1

        boxes = 0
        next_ann = coco.next_id(coco_data["annotations"])
        for a in body.annotations:
            img = img_map.get(a.image_id)
            bbox = img and coco.clip_box(*a.bbox, img["width"], img["height"])
            if not bbox:
                continue
            coco_data["annotations"].append({
                "id": next_ann, "image_id": img["id"], "category_id": cat_map[a.category_id],
                "bbox": bbox, "area": round(bbox[2] * bbox[3], 2), "segmentation": [], "iscrowd": 0,
            })
            next_ann += 1
            boxes += 1

    logger.info(f"Imported labels for {len(img_map)} image(s), {boxes} box(es); skipped {skipped} already labelled")
    return {"images": len(img_map), "boxes": boxes, "classes_added": classes_added,
            "skipped_images": skipped, "not_in_workspace": missing}


def _export_files(fmt: ExportFormat, val: float, seed: int, yaml_path: str | None) -> tuple[Files, str]:
    """Everything an export contains, plus the fingerprint of the labels it was made from."""
    with coco.LOCK:   # the labels and their fingerprint from the same moment
        data, fingerprint = coco.snapshot(), coco.fingerprint()
    names = coco.list_dataset_images()
    if fmt == ExportFormat.yolo:
        files, _ = yolo.yolo_files(data, names, val, seed, yaml_path)
    else:
        files = [("coco_annotations.json", coco.dumps(exports.coco_for_export(data, names)).encode("utf-8"))]
        if fmt == ExportFormat.coco_images:
            # every workspace image, labelled or not, so the folder can be imported again to carry on
            files += [(f"images/{name}", coco.ANNOT_IMAGES_DIR / name) for name in names]
    return files, fingerprint


def _export_name(fmt: ExportFormat) -> str:
    return f"box-annotator-{fmt.value.replace('_', '-')}-{time.strftime('%Y%m%d-%H%M%S')}"


def _sweep_downloads(max_age: float = 3600) -> None:
    """Delete prepared downloads the browser never fetched."""
    if DOWNLOADS.exists():
        for folder in DOWNLOADS.iterdir():
            try:
                if time.time() - folder.stat().st_mtime > max_age:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


def _downloads_dir() -> Path:
    """
    The folder for prepared downloads, readable only by this user: an export holds
    their images, and on Linux and macOS the temp folder is shared with everyone.
    """
    DOWNLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        st = DOWNLOADS.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():   # someone else made it first
            raise HTTPException(status_code=500, detail=f"{DOWNLOADS} belongs to another user; delete it or set TMPDIR")
        if st.st_mode & 0o077:
            DOWNLOADS.chmod(0o700)
    return DOWNLOADS


def _prepare_download(fmt: ExportFormat, val: float, seed: int) -> Path:
    """Build an export in a temp folder: the .json for COCO, otherwise a .zip."""
    _sweep_downloads()
    folder = _downloads_dir() / secrets.token_hex(8)
    folder.mkdir()
    try:
        name = _export_name(fmt)
        with _exporting():
            files, fingerprint = _export_files(fmt, val, seed, yaml_path=None)
            if fmt == ExportFormat.coco:
                out = folder / f"{name}.json"
                out.write_bytes(files[0][1])
            else:
                out = folder / f"{name}.zip"
                exports.write_zip(files, out, root=name)
        (folder / EXPORT_MARK).write_text(fingerprint, encoding="utf-8")
    except ValueError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except OSError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise _write_failed(e) from e
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return out


def _serve(out: Path, request: Request) -> StreamingResponse:
    """
    Send a prepared export. Only once every byte went to a client that stayed
    connected does it count as exported, and its temp copy go: Clear must still
    warn after a download cancelled half-way. Byte ranges aren't offered, so a
    download manager can't fetch one piece and leave the rest.
    """
    finished = False

    async def body():
        nonlocal finished
        with out.open("rb") as f:
            while chunk := await run_in_threadpool(f.read, CHUNK):
                if await request.is_disconnected():
                    return
                yield chunk
        finished = True

    def after():
        if finished:
            _mark_exported((out.parent / EXPORT_MARK).read_text(encoding="utf-8"))
            shutil.rmtree(out.parent, ignore_errors=True)
        else:   # kept for the browser's Retry; swept after an hour
            logger.info(f"The download of {out.name} stopped before the end, so it doesn't count as exported")

    headers = {"Content-Length": str(out.stat().st_size), "Content-Disposition": f'attachment; filename="{out.name}"'}
    media = "application/zip" if out.suffix == ".zip" else "application/json"
    return StreamingResponse(body(), media_type=media, headers=headers, background=BackgroundTask(after))


@router.get("/api/export/{fmt}")
def download_export(request: Request, fmt: ExportFormat, val: float = Query(0.2, ge=0, lt=1), seed: int = 0):
    """Download an export: COCO as .json, the others as a .zip."""
    return _serve(_prepare_download(fmt, val, seed), request)


@router.post("/api/export/{fmt}/download")
def prepare_download(fmt: ExportFormat, body: ExportOptions):
    """
    Build an export for the browser to download from the returned url. The
    browser then saves it straight to disk, however big, instead of holding it in memory.
    """
    out = _prepare_download(fmt, body.val, body.seed)
    return {"url": f"api/downloads/{out.parent.name}/{out.name}", "name": out.name}


@router.get("/api/downloads/{token}/{name}")
def fetch_download(token: str, name: str, request: Request):
    """A prepared export. It's deleted once fully sent, so each url works once."""
    out = DOWNLOADS / token / name
    if not TOKEN.fullmatch(token) or Path(name).name != name or not out.is_file():
        raise HTTPException(status_code=404, detail="This download has expired; export again")
    return _serve(out, request)


@router.post("/api/export/{fmt}")
def export_to_folder(fmt: ExportFormat, body: ExportRequest, request: Request):
    """Write an export into a new file or subfolder of a folder on this computer."""
    _local_only(request)
    folder = Path(body.folder).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise HTTPException(status_code=422, detail=f"Folder not found: {body.folder}")
    folder, src = folder.resolve(), coco.ANNOT_IMAGES_DIR.resolve()
    if folder == src or src in folder.parents:
        raise HTTPException(status_code=422, detail="Pick a folder outside the workspace's images")
    name, ext = _export_name(fmt), ".json" if fmt == ExportFormat.coco else ""
    out, n = folder / f"{name}{ext}", 2
    while out.exists():   # two exports in the same second
        out, n = folder / f"{name.replace('box-annotator-', f'box-annotator-{n}-', 1)}{ext}", n + 1
    try:
        with _exporting():
            files, fingerprint = _export_files(fmt, body.val, body.seed, yaml_path=out.as_posix())
            if fmt == ExportFormat.coco:
                out.write_bytes(files[0][1])
            else:
                exports.write_folder(files, out)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except BaseException as e:
        # e.g. the disk filled up: don't leave half an export behind
        if out.is_dir():
            shutil.rmtree(out, ignore_errors=True)
        else:
            out.unlink(missing_ok=True)
        if isinstance(e, OSError):
            raise _write_failed(e) from e
        raise
    _mark_exported(fingerprint)
    logger.info(f"Exported {fmt.value} to {out}")
    return {"path": str(out)}


@router.post("/api/pick-folder")
def pick_folder(body: PickFolderRequest, request: Request):
    """Show this computer's own folder picker; returns the chosen path, or null if cancelled."""
    _local_only(request)
    if not _picker_open.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="The folder picker is already open; it may be behind the browser window")
    try:
        r = subprocess.run([sys.executable, "-c", PICKER, "Choose where to save the export", body.start or ""],
                           capture_output=True, text=True, encoding="utf-8")
    except OSError:
        r = None
    finally:
        _picker_open.release()
    if r is None or r.returncode != 0:
        raise HTTPException(status_code=501, detail="No folder picker here; type the folder path instead")
    path = r.stdout.strip()
    return {"path": str(Path(path)) if path else None}


@router.post("/api/workspace/clear")
def clear_workspace(body: ClearRequest, request: Request):
    """
    Delete the imported images and their labels to free the space. Only from this
    computer, refused for the user's own folder, and needs `force` if labels changed
    since the last export or an unreadable labels file was set aside.
    """
    _local_only(request, "Clearing the workspace only works on the computer running the app")
    if not config.MANAGED_WORKSPACE:
        raise HTTPException(status_code=403, detail="This workspace is your own folder (ANNOTATOR_DATASET_DIR), "
                                                    "so clearing is off to keep your images safe")
    with coco.LOCK:   # no save or new export can slip in between the checks and the delete
        if _exports_running:
            raise HTTPException(status_code=409, detail="An export is still being written; clear once it's done")
        coco_data = coco.load_coco()
        set_aside = coco.set_aside()
        if not body.force and _unexported(set_aside):
            raise HTTPException(status_code=409, detail=f"An unreadable labels file was set aside: {set_aside[-1]}" if set_aside
                                else "Some labels changed since the last export")
        deleted, in_use = 0, []
        if coco.ANNOT_IMAGES_DIR.exists():
            for p in coco.ANNOT_IMAGES_DIR.iterdir():
                is_image = p.suffix.lower() in coco.ANNOT_EXTS
                if not p.is_file() or not (is_image or p.suffix == ".part"):   # .part: an upload cut off half-way
                    continue
                try:
                    coco.retry(partial(p.unlink, missing_ok=True))
                except OSError:   # open in another program (Windows can't delete it): keep it, and say so
                    if is_image:
                        in_use.append(p.name)
                    continue
                deleted += is_image
        fresh = coco.empty_coco()
        if body.keep_classes:
            fresh["categories"] = coco_data["categories"]
        coco.save_coco(fresh)
        (coco.DATASET_DIR / EXPORT_MARK).unlink(missing_ok=True)
    logger.info(f"Cleared the workspace: {deleted} image(s) deleted, {len(in_use)} in use and kept")
    return {"deleted_images": deleted, "in_use": in_use}
