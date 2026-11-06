"""
COCO -> YOLO export.

Turns the annotator's COCO file into the folder layout Ultralytics YOLO
trains on:

  <out>/images/{train,val}/   copies of the saved images
  <out>/labels/{train,val}/   one .txt per image: "class xc yc w h", normalised to 0-1
  <out>/data.yaml             dataset config (paths + class names)

Only images saved in the annotator are exported. Reviewed negatives (saved
with 0 boxes) get an empty label file, which YOLO treats as background.

Run with:  python -m app.yolo [--out DIR] [--val 0.2] [--seed 0] [--overwrite]
"""

import argparse
import functools
import json
import random
import shutil
from collections.abc import Iterable
from pathlib import Path

from . import coco
from .config import logger
from .exports import Files, write_folder

SPLITS = ("train", "val")


def yolo_line(class_idx: int, bbox: list[float], width: int, height: int) -> str:
    """One YOLO label line from a COCO [x, y, w, h] pixel box."""
    x, y, w, h = bbox
    return f"{class_idx} {(x + w / 2) / width:.6f} {(y + h / 2) / height:.6f} {w / width:.6f} {h / height:.6f}"


def _prepare_out_dir(out_dir: Path, overwrite: bool) -> None:
    """Refuse an output folder that overlaps the source images or holds a previous export."""
    src = coco.ANNOT_IMAGES_DIR.resolve()
    if out_dir == src or out_dir in src.parents or src in out_dir.parents:
        raise ValueError(f"Output folder {out_dir} overlaps the source images in {src}; pick another one")
    if out_dir.exists() and any(out_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"{out_dir} is not empty; pass --overwrite to replace a previous export")
        # remove only what an export writes, so stale labels can't linger
        for split in SPLITS:
            shutil.rmtree(out_dir / "images" / split, ignore_errors=True)
            shutil.rmtree(out_dir / "labels" / split, ignore_errors=True)
            (out_dir / "labels" / f"{split}.cache").unlink(missing_ok=True)   # written by Ultralytics
        (out_dir / "data.yaml").unlink(missing_ok=True)


def yolo_files(data: dict, names: Iterable[str], val_fraction: float = 0.2, seed: int = 0,
               path: str | None = None) -> tuple[Files, dict[str, int]]:
    """
    Every file of a YOLO export of the saved images and their boxes.

    Args:
        data: the COCO data.
        names: the images in the workspace; saved images missing from it are skipped.
        val_fraction: share of images held out for validation; 0 puts everything in train.
        seed: shuffle seed, so the same COCO file always gives the same split.
        path: the dataset folder for data.yaml. Without one (a zip, which can be
            unzipped anywhere) Ultralytics uses data.yaml's own folder.

    Returns:
        The files as (path in the export, image to copy or file contents), and
        the image counts per split plus the total number of boxes.
    """
    if not 0 <= val_fraction < 1:
        raise ValueError("val_fraction must be at least 0 and below 1")

    # COCO category ids -> 0-based YOLO class indices
    categories = sorted(data["categories"], key=lambda c: c["id"])
    class_idx = {c["id"]: i for i, c in enumerate(categories)}

    present = set(names)
    by_id = {img["id"]: img for img in data["images"]}
    lines: dict[int, list[str]] = {img_id: [] for img_id in by_id}
    skipped = 0
    for ann in data["annotations"]:
        img = by_id.get(ann["image_id"])
        if img is None or ann["category_id"] not in class_idx:
            skipped += 1
            continue
        if img["file_name"] not in present:
            continue   # skipped below, with a warning
        w, h = img.get("width"), img.get("height")
        if not (isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0):
            # a labels file from another tool can leave the size out; YOLO boxes are fractions of it
            raise ValueError(f"The labels file has no image size for {img['file_name']}; open it in the annotator and Save it")
        lines[img["id"]].append(yolo_line(class_idx[ann["category_id"]], ann["bbox"], w, h))
    if skipped:
        logger.warning(f"Skipped {skipped} annotation(s) with an unknown image or category")

    records = []
    stems: dict[str, str] = {}
    for img in sorted(data["images"], key=lambda i: i["file_name"]):
        name = img["file_name"]
        if name not in present:
            logger.warning(f"Skipping {name}: not found in {coco.ANNOT_IMAGES_DIR}")
            continue
        # YOLO finds labels by swapping the extension for .txt; lower() because Windows ignores case
        stem = Path(name).stem.lower()
        if stem in stems:
            raise ValueError(f"{stems[stem]} and {name} would share the label file {Path(name).stem}.txt; rename one")
        stems[stem] = name
        records.append(img)
    if not records:
        raise ValueError("No saved images to export; label some in the annotator first")

    order = list(records)
    random.Random(seed).shuffle(order)
    # at least one image each side once there are two; never all of them in val
    n_val = min(max(1, round(len(order) * val_fraction)), len(order) - 1) if val_fraction and len(order) > 1 else 0
    split_of = {img["id"]: "val" if k < n_val else "train" for k, img in enumerate(order)}

    files: Files = []
    counts = {"train": 0, "val": 0, "boxes": 0}
    for img in records:
        split, name = split_of[img["id"]], img["file_name"]
        rows = lines[img["id"]]
        files.append((f"images/{split}/{name}", coco.ANNOT_IMAGES_DIR / name))
        # bytes, so the labels have \n line endings on every OS
        files.append((f"labels/{split}/{Path(name).stem}.txt", "".join(row + "\n" for row in rows).encode("utf-8")))
        counts[split] += 1
        counts["boxes"] += len(rows)

    # JSON strings are valid YAML, so names and path need no YAML library. ensure_ascii=False:
    # JSON writes an emoji as a surrogate pair ("\ud83d\ude00"), which YAML reads as two broken halves
    quote = functools.partial(json.dumps, ensure_ascii=False)
    class_names = "".join(f"  {i}: {quote(c['name'])}\n" for i, c in enumerate(categories))
    where = (f"# written by Box Annotator; update `path` if you move this folder\npath: {quote(path)}\n" if path else
             "# written by Box Annotator; no `path`, so Ultralytics uses this file's folder wherever you unzip it\n")
    files.append(("data.yaml", (
        where +
        "train: images/train\n"
        f"val: images/{'val' if counts['val'] else 'train'}\n"
        f"names:\n{class_names}"
    ).encode("utf-8")))
    return files, counts


def export_yolo(out_dir: Path, val_fraction: float = 0.2, seed: int = 0, overwrite: bool = False) -> dict[str, int]:
    """
    Export every saved image and its boxes as a YOLO dataset folder.

    Args:
        out_dir: folder to write the dataset to (created if missing).
        val_fraction: share of images held out for validation; 0 puts everything in train.
        seed: shuffle seed, so the same COCO file always gives the same split.
        overwrite: replace a previous export in out_dir instead of refusing.

    Returns:
        Image counts per split plus the total number of boxes written.
    """
    out_dir = Path(out_dir).resolve()
    files, counts = yolo_files(coco.snapshot(), coco.list_dataset_images(), val_fraction, seed, out_dir.as_posix())
    _prepare_out_dir(out_dir, overwrite)
    write_folder(files, out_dir)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.yolo",
                                     description="Export the annotator's COCO file as a YOLO dataset.")
    parser.add_argument("--out", type=Path, help="output folder (default: <dataset>/yolo)")
    parser.add_argument("--val", type=float, default=0.2, help="share of images for validation (default: 0.2)")
    parser.add_argument("--seed", type=int, default=0, help="shuffle seed for the train/val split (default: 0)")
    parser.add_argument("--overwrite", action="store_true", help="replace a previous export in the output folder")
    args = parser.parse_args(argv)

    out_dir = args.out or coco.DATASET_DIR / "yolo"
    try:
        counts = export_yolo(out_dir, args.val, args.seed, args.overwrite)
    except (ValueError, FileExistsError) as e:
        parser.error(str(e))
    logger.info(f"Exported {counts['train']} train / {counts['val']} val images "
                f"with {counts['boxes']} box(es) to {Path(out_dir).resolve()}")


if __name__ == "__main__":
    main()
