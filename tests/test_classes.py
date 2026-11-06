"""Class tests: add, rename, delete, per-box classes and files from the single-class version."""

import json

from app import coco


def add(client, name):
    return client.post("/api/categories", json={"name": name})


def put(client, name, boxes):
    return client.put(f"/api/annotations/{name}", json={"width": 100, "height": 80, "boxes": boxes})


def test_fresh_dataset_has_no_classes(client):
    assert client.get("/api/categories").json() == []
    assert client.get("/api/coco").json()["categories"] == []


def test_add_rename_delete(client):
    assert add(client, "  enamel caries ").json() == {"id": 1, "name": "enamel caries"}
    assert add(client, "dentin caries").status_code == 201
    assert client.patch("/api/categories/2", json={"name": "deep caries"}).json() == {"id": 2, "name": "deep caries"}
    assert client.delete("/api/categories/1").status_code == 204
    assert client.get("/api/categories").json() == [{"id": 2, "name": "deep caries", "box_count": 0}]
    assert coco.load_coco()["categories"] == [{"id": 2, "name": "deep caries", "supercategory": ""}]


def test_names_must_be_unique_and_not_blank(client):
    add(client, "Car")
    add(client, "bus")
    assert add(client, "car ").status_code == 409
    assert add(client, "   ").status_code == 422
    assert client.patch("/api/categories/2", json={"name": "CAR"}).status_code == 409
    assert client.patch("/api/categories/1", json={"name": "CAR"}).status_code == 200   # its own name, new case
    assert client.patch("/api/categories/9", json={"name": "van"}).status_code == 404
    assert [c["name"] for c in client.get("/api/categories").json()] == ["CAR", "bus"]


def test_boxes_keep_their_class(client, make_image):
    make_image("a.png")
    add(client, "car")
    add(client, "bus")
    boxes = [{"x": 1, "y": 1, "w": 5, "h": 5, "category_id": 2}, {"x": 9, "y": 9, "w": 5, "h": 5, "category_id": 1}]
    assert put(client, "a.png", boxes).status_code == 200
    assert client.get("/api/annotations/a.png").json()["boxes"] == boxes
    assert [c["box_count"] for c in client.get("/api/categories").json()] == [1, 1]


def test_unknown_or_missing_class_is_rejected(client, make_image):
    make_image("a.png")
    add(client, "car")
    r = put(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5, "category_id": 7}])
    assert r.status_code == 422 and "add the class first" in r.json()["detail"]
    assert put(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}]).status_code == 422
    assert coco.load_coco()["images"] == []


def test_class_in_use_cannot_be_deleted(client, make_image):
    make_image("a.png")
    add(client, "car")
    put(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5, "category_id": 1}])
    r = client.delete("/api/categories/1")
    assert r.status_code == 409 and "used by 1 box" in r.json()["detail"]
    assert client.get("/api/annotations/a.png").json()["boxes"][0]["category_id"] == 1
    assert client.delete("/api/categories/5").status_code == 404


def test_file_from_the_single_class_version_still_works(client, make_image):
    """Old boxes keep their class; a box whose class isn't listed gets a placeholder instead of getting stuck."""
    make_image("a.png")
    ann = {"image_id": 1, "area": 25, "segmentation": [], "iscrowd": 0}
    coco.COCO_PATH.write_text(json.dumps({
        "images": [{"id": 1, "file_name": "a.png", "width": 100, "height": 80}],
        "annotations": [{"id": 1, "category_id": 1, "bbox": [1, 1, 5, 5], **ann},
                        {"id": 2, "category_id": 3, "bbox": [9, 9, 5, 5], **ann}],
        "categories": [{"id": 1, "name": "cavity", "supercategory": "lesion"}],
    }), encoding="utf-8")
    assert client.get("/api/categories").json() == [{"id": 1, "name": "cavity", "box_count": 1},
                                                    {"id": 3, "name": "class 3", "box_count": 1}]
    assert add(client, "enamel caries").json()["id"] == 4
    boxes = client.get("/api/annotations/a.png").json()["boxes"]
    assert put(client, "a.png", boxes).status_code == 200      # re-saving the old boxes as they are works
