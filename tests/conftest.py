"""
Shared fixtures. Every test gets its own throwaway dataset folder, so the
suite never touches a real dataset.
"""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from app import coco, config, workspace
from app.main import app


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Keep each phase's report on the test, so a fixture can tell that its test failed."""
    report = (yield).get_result()
    setattr(item, f"report_{report.when}", report)


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """Point the annotator at an empty temp dataset folder and return it."""
    monkeypatch.setattr(coco, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(coco, "ANNOT_IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(coco, "COCO_PATH", tmp_path / "coco_annotations.json")
    monkeypatch.setattr(config, "MANAGED_WORKSPACE", True)
    monkeypatch.setattr(workspace, "DOWNLOADS", tmp_path / "downloads")   # not the real temp folder
    monkeypatch.setattr(coco, "RETRY_DELAY", 0)                           # a locked file is retried at once
    return tmp_path


@pytest.fixture
def server(dataset):
    """Run the app on a free local port for the duration of one test."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))   # handed to uvicorn bound, so nothing can take the port in between
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("the test server didn't start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    srv.should_exit = True
    thread.join(5)
    sock.close()


@pytest.fixture
def make_image(dataset):
    """Factory: write a grey test image of the given size into the dataset."""
    def make(name, size=(100, 80)):
        images = dataset / "images"
        images.mkdir(exist_ok=True)
        path = images / name
        if path.suffix.lower() in coco.ANNOT_EXTS:
            Image.new("RGB", size, (90, 90, 90)).save(path)
        else:
            path.write_bytes(b"not an image the UI can show")
        return path
    return make


@pytest.fixture
def client(dataset):
    """A client calling from this computer, like the browser running the UI."""
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def seed(*records, classes=("object",)):
    """
    Pre-save classes and boxes: seed(("a.png", 400, 300, [[x, y, w, h], ...]), ...).
    Classes get ids 1, 2, ...; a box can add a 5th item with its class id (default 1).
    """
    data = coco.empty_coco()
    data["categories"] = [{"id": i, "name": n, "supercategory": ""} for i, n in enumerate(classes, start=1)]
    for img_id, (name, w, h, boxes) in enumerate(records, start=1):
        data["images"].append({"id": img_id, "file_name": name, "width": w, "height": h})
        for x, y, bw, bh, *cat in boxes:
            data["annotations"].append({"id": len(data["annotations"]) + 1, "image_id": img_id,
                                        "category_id": cat[0] if cat else 1, "bbox": [x, y, bw, bh],
                                        "area": bw * bh, "segmentation": [], "iscrowd": 0})
    coco.save_coco(data)


def stored(dataset):
    """Saved boxes per file name, as [x, y, w, h] without their class (the app re-reads the file if it changed)."""
    data = coco.load_coco()
    return {
        img["file_name"]: [a["bbox"] for a in data["annotations"] if a["image_id"] == img["id"]]
        for img in data["images"]
    }
