"""
HTTP layer for the annotator.

  GET    /                              the annotator UI
  GET    /static/{style.css|js/*.js}    its stylesheet and script modules
  GET    /api/images                    dataset images + annotation status
  GET    /api/images/{file_name}        raw image bytes for the canvas
  GET    /api/annotations/{file_name}   saved boxes for one image
  PUT    /api/annotations/{file_name}   replace all boxes for one image
  GET    /api/categories                classes + how many saved boxes use each
  POST   /api/categories                add a class
  PATCH  /api/categories/{id}           rename a class
  DELETE /api/categories/{id}           delete a class no saved box uses
  GET    /api/coco                      download the COCO file

Routes are plain `def`, so FastAPI runs them in worker threads and a slow save
never holds up image loading. coco.LOCK keeps the COCO reads and changes from
interleaving. Importing, exporting and clearing live in workspace.py.
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import coco
from .config import logger
from .schemas import CategoryRequest, SaveRequest

STATIC_DIR = Path(__file__).resolve().parent / "static"
UI_PATH = STATIC_DIR / "index.html"
STATIC_NAME = re.compile(r"style\.css|js/\w+\.js")   # what the page loads besides itself

router = APIRouter()


def _dataset_image(file_name: str) -> Path:
    """
    Resolve a plain file name to an annotatable image in the dataset folder.
    Rejects anything with a path component (no traversal) or an unsupported type.
    """
    safe = Path(file_name).name
    path = coco.ANNOT_IMAGES_DIR / safe
    if safe != file_name or not path.is_file() or path.suffix.lower() not in coco.ANNOT_EXTS:
        raise HTTPException(status_code=404, detail="Image not found in dataset folder")
    return path


def _category(coco_data: dict, category_id: int) -> dict:
    """Find a class by id, or 404."""
    cat = next((c for c in coco_data["categories"] if c["id"] == category_id), None)
    if cat is None:
        raise HTTPException(status_code=404, detail="Class not found")
    return cat


def _clean_name(name: str, coco_data: dict, except_id: int | None = None) -> str:
    """Trim a class name and reject blanks or duplicates (ignoring case)."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Class name can't be empty")
    if any(c["name"].lower() == name.lower() and c["id"] != except_id for c in coco_data["categories"]):
        raise HTTPException(status_code=409, detail=f"A class called '{name}' already exists")
    return name


@router.get("/", include_in_schema=False)
def annotate_ui():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-cache"})   # always the current UI after an update


@router.get("/static/{name:path}", include_in_schema=False)
def ui_file(name: str):
    """The UI's scripts and stylesheet, never cached stale, so they always match the page."""
    path = STATIC_DIR / name
    if not STATIC_NAME.fullmatch(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    # the type is set here: Windows may map .js to text/plain, and browsers refuse to run a module served so
    kind = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "no-cache"})


@router.get("/api/images")
def list_images():
    """
    List dataset images with their annotation status and box counts.
    An image counts as annotated once it has been saved, even with 0 boxes
    (a reviewed negative), so the UI doesn't keep sending you back to it.
    """
    names = coco.list_dataset_images()
    with coco.LOCK:
        coco_data = coco.load_coco()
        name_to_id = {img["file_name"]: img["id"] for img in coco_data["images"]}
        counts: dict[int, int] = {}
        for ann in coco_data["annotations"]:
            counts[ann["image_id"]] = counts.get(ann["image_id"], 0) + 1

    items = []
    for name in names:
        img_id = name_to_id.get(name)
        items.append({
            "file_name": name,
            "annotated": img_id is not None,
            "box_count": counts.get(img_id, 0) if img_id is not None else 0,
        })

    # a JSONResponse skips FastAPI's generic encoder, which is slow on a list of thousands
    return JSONResponse({
        "total": len(items),
        "annotated": sum(1 for it in items if it["annotated"]),
        "images": items,
    })


@router.get("/api/images/{file_name}")
def get_image(file_name: str):
    """Serve a single dataset image (raw bytes) for the canvas."""
    return FileResponse(str(_dataset_image(file_name)))


@router.get("/api/annotations/{file_name}")
def get_annotations(file_name: str):
    """Return existing boxes (with their class) for one image so the user can re-edit."""
    safe = Path(file_name).name
    with coco.LOCK:
        coco_data = coco.load_coco()
        img = next((i for i in coco_data["images"] if i["file_name"] == safe), None)
        if img is None:
            return {"file_name": safe, "boxes": []}
        boxes = [
            {"x": a["bbox"][0], "y": a["bbox"][1], "w": a["bbox"][2], "h": a["bbox"][3], "category_id": a["category_id"]}
            for a in coco_data["annotations"] if a["image_id"] == img["id"]
        ]
    return {"file_name": safe, "width": img.get("width"), "height": img.get("height"), "boxes": boxes}


@router.put("/api/annotations/{file_name}")
def save_annotations(file_name: str, body: SaveRequest):
    """
    Save (replace) all boxes for one image into the COCO file.
    Re-saving an image overwrites its previous boxes so edits are idempotent.
    """
    with coco.edit() as coco_data:
        _dataset_image(file_name)   # inside the lock: a Clear may have deleted it while this save waited
        known = {c["id"] for c in coco_data["categories"]}
        unknown = sorted({b.category_id for b in body.boxes} - known)
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown class id(s) {unknown}; add the class first")

        # find or create the image record
        img = next((i for i in coco_data["images"] if i["file_name"] == file_name), None)
        if img is None:
            img = {"id": coco.next_id(coco_data["images"]), "file_name": file_name, "width": body.width, "height": body.height}
            coco_data["images"].append(img)
        else:
            img["width"] = body.width
            img["height"] = body.height

        # drop this image's old annotations, then re-add the current boxes
        coco_data["annotations"] = [a for a in coco_data["annotations"] if a["image_id"] != img["id"]]
        next_ann_id = coco.next_id(coco_data["annotations"])

        saved = 0
        for box in body.boxes:
            # clip to the image so out-of-bounds boxes never reach the dataset
            bbox = coco.clip_box(box.x, box.y, box.w, box.h, body.width, body.height)
            if bbox is None:
                continue
            coco_data["annotations"].append({
                "id": next_ann_id,
                "image_id": img["id"],
                "category_id": box.category_id,
                "bbox": bbox,
                "area": round(bbox[2] * bbox[3], 2),
                "segmentation": [],
                "iscrowd": 0,
            })
            next_ann_id += 1
            saved += 1

    logger.info(f"Saved {saved} box(es) for {file_name}")
    return {"file_name": file_name, "saved_boxes": saved}


@router.get("/api/categories")
def list_categories():
    """The dataset's classes in id order, each with how many saved boxes use it."""
    with coco.LOCK:
        coco_data = coco.load_coco()
        counts: dict[int, int] = {}
        for ann in coco_data["annotations"]:
            counts[ann["category_id"]] = counts.get(ann["category_id"], 0) + 1
        return [
            {"id": c["id"], "name": c["name"], "box_count": counts.get(c["id"], 0)}
            for c in sorted(coco_data["categories"], key=lambda c: c["id"])
        ]


@router.post("/api/categories", status_code=201)
def add_category(body: CategoryRequest):
    """Add a class. Existing ids are never renumbered, so saved boxes keep their class."""
    with coco.edit() as coco_data:
        name = _clean_name(body.name, coco_data)
        cat_id = coco.next_id(coco_data["categories"])
        coco_data["categories"].append({"id": cat_id, "name": name, "supercategory": ""})
    logger.info(f"Added class {cat_id}: {name}")
    return {"id": cat_id, "name": name}


@router.patch("/api/categories/{category_id}")
def rename_category(category_id: int, body: CategoryRequest):
    """Rename a class. Its boxes follow, since they point at the id rather than the name."""
    with coco.edit() as coco_data:
        cat = _category(coco_data, category_id)
        cat["name"] = _clean_name(body.name, coco_data, except_id=category_id)
    return {"id": category_id, "name": cat["name"]}


@router.delete("/api/categories/{category_id}", status_code=204)
def delete_category(category_id: int):
    """Delete a class, but only while no saved box uses it, so no label is ever lost."""
    with coco.edit() as coco_data:
        cat = _category(coco_data, category_id)
        used = sum(1 for a in coco_data["annotations"] if a["category_id"] == category_id)
        if used:
            raise HTTPException(status_code=409, detail=f"'{cat['name']}' is used by {used} box(es); change or delete them first")
        coco_data["categories"].remove(cat)


@router.get("/api/coco")
def download_coco():
    """Download the COCO file (an empty skeleton if nothing has been saved yet)."""
    headers = {"Content-Disposition": 'attachment; filename="coco_annotations.json"'}
    return JSONResponse(coco.snapshot(), headers=headers)
