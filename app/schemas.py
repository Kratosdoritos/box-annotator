"""Pydantic request models for the annotator API."""

import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def _whole_characters(text: str) -> str:
    """Half an emoji (a lone surrogate) becomes �: JSON can carry it, but it can't be saved as UTF-8."""
    return re.sub("[\ud800-\udfff]", "\ufffd", text)


Text = Annotated[str, AfterValidator(_whole_characters)]   # text that ends up in the labels file


class Box(BaseModel):
    """A single labelled bounding box in image-pixel coordinates (COCO xywh)."""
    x: float = Field(..., ge=0)   # top-left x
    y: float = Field(..., ge=0)   # top-left y
    w: float = Field(..., gt=0)   # width  (must be > 0)
    h: float = Field(..., gt=0)   # height (must be > 0)
    category_id: int              # one of the dataset's classes


class SaveRequest(BaseModel):
    """Body of PUT /api/annotations/{file_name} — replaces all boxes for one image."""
    width: int = Field(..., gt=0)                       # image width in pixels
    height: int = Field(..., gt=0)                      # image height in pixels
    boxes: list[Box] = Field(default_factory=list)      # the full set of boxes for this image


class CategoryRequest(BaseModel):
    """Body of POST /api/categories and PATCH /api/categories/{id}."""
    name: Text = Field(..., max_length=64)   # surrounding spaces are trimmed; must not end up empty


class ExportFormat(str, Enum):
    coco = "coco"                 # labels only, one .json
    coco_images = "coco_images"   # labels + every workspace image; can be imported again
    yolo = "yolo"                 # Ultralytics layout with a train/val split


class ExportOptions(BaseModel):
    """Body of POST /api/export/{format}/download: prepare an export for the browser to download."""
    val: float = Field(0.2, ge=0, lt=1)          # YOLO only: share of images for validation
    seed: int = 0                                # YOLO only: shuffle seed for the split


class ExportRequest(ExportOptions):
    """Body of POST /api/export/{format}: write the export into a folder on this computer."""
    folder: str = Field(..., min_length=1)       # an existing folder; the export goes in a new file/subfolder


class PickFolderRequest(BaseModel):
    """Body of POST /api/pick-folder."""
    start: str | None = None   # folder the picker opens in


class ClearRequest(BaseModel):
    """Body of POST /api/workspace/clear."""
    keep_classes: bool = True                    # keep the class list for the next batch
    force: bool = False                          # clear even if labels changed since the last export


class CocoImage(BaseModel):
    id: int
    file_name: Text
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)


class CocoAnnotation(BaseModel):
    image_id: int
    category_id: int
    bbox: list[float] = Field(..., min_length=4, max_length=4)


class CocoCategory(BaseModel):
    id: int
    name: Text


class CocoImport(BaseModel):
    """Body of POST /api/import/coco: any COCO detection file (extra fields are ignored)."""
    images: list[CocoImage]
    annotations: list[CocoAnnotation] = Field(default_factory=list)
    categories: list[CocoCategory] = Field(default_factory=list)
