"""
Writing exports into a folder or a zip.

An export is a list of (path inside the export, source) pairs, where the source
is an image to copy or the bytes of a generated file. The same list serves a
folder or a zip, and a zip is written straight from the workspace images
without copying them anywhere first.
"""

import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path

Files = list[tuple[str, Path | bytes]]


def coco_for_export(data: dict, names: Iterable[str]) -> dict:
    """The COCO data limited to the images in `names` (the ones actually in the workspace)."""
    present = set(names)
    images = [i for i in data["images"] if i["file_name"] in present]
    ids = {i["id"] for i in images}
    return {**data, "images": images, "annotations": [a for a in data["annotations"] if a["image_id"] in ids]}


def write_folder(files: Files, out_dir: Path) -> None:
    """Write the files under out_dir, creating folders as needed."""
    made = set()
    for rel, src in files:
        dst = out_dir / rel
        if dst.parent not in made:
            dst.parent.mkdir(parents=True, exist_ok=True)
            made.add(dst.parent)
        if isinstance(src, bytes):
            dst.write_bytes(src)
        else:
            shutil.copy2(src, dst)


def write_zip(files: Files, zip_path: Path, root: str) -> None:
    """Write the files into a new zip, under a top folder called `root`."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for rel, src in files:
            if isinstance(src, bytes):
                zf.writestr(f"{root}/{rel}", src, compress_type=zipfile.ZIP_DEFLATED)
            else:   # images are compressed already: storing them is as small and much faster
                zf.write(src, f"{root}/{rel}", compress_type=zipfile.ZIP_STORED)
