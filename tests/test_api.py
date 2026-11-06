"""API tests: listing, saving, clipping, path safety, corrupt-file recovery and parallel saves."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import coco, main, routes
from app.main import app
from app.schemas import Box, SaveRequest

from conftest import seed, stored


@pytest.fixture(autouse=True)
def one_class(dataset):
    """Every test here labels with class 1, "object"."""
    seed()


def save(client, name, boxes, size=(100, 80)):
    boxes = [{"category_id": 1, **b} for b in boxes]
    return client.put(f"/api/annotations/{name}", json={"width": size[0], "height": size[1], "boxes": boxes})


def test_missing_dataset_folder_lists_nothing(client):
    assert client.get("/api/images").json() == {"total": 0, "annotated": 0, "images": []}


def test_lists_only_images_a_browser_can_show(client, make_image):
    for name in ["b.jpg", "a.png", "c.bmp", "d.tif", "notes.txt"]:
        make_image(name)
    names = [i["file_name"] for i in client.get("/api/images").json()["images"]]
    assert names == ["a.png", "b.jpg", "c.bmp"]


def test_list_is_in_natural_order(client, make_image):
    for name in ["img10.png", "img2.png", "Img1.png", "b.png"]:
        make_image(name)
    names = [i["file_name"] for i in client.get("/api/images").json()["images"]]
    assert names == ["b.png", "Img1.png", "img2.png", "img10.png"]


def test_save_then_reload_boxes(client, make_image):
    make_image("a.png")
    r = save(client, "a.png", [{"x": 10, "y": 10, "w": 20, "h": 20}])
    assert r.status_code == 200 and r.json()["saved_boxes"] == 1
    got = client.get("/api/annotations/a.png").json()
    assert got["boxes"] == [{"x": 10, "y": 10, "w": 20, "h": 20, "category_id": 1}]
    assert (got["width"], got["height"]) == (100, 80)


def test_out_of_bounds_boxes_are_clipped_or_dropped(client, make_image, dataset):
    make_image("a.png")
    r = save(client, "a.png", [
        {"x": 90, "y": 70, "w": 50, "h": 50},   # partly outside -> clipped to 10x10
        {"x": 100, "y": 5, "w": 5, "h": 5},     # fully outside  -> dropped
    ])
    assert r.json()["saved_boxes"] == 1
    assert stored(dataset)["a.png"] == [[90, 70, 10, 10]]


def test_resave_replaces_previous_boxes(client, make_image, dataset):
    make_image("a.png")
    make_image("b.png")
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}, {"x": 20, "y": 20, "w": 5, "h": 5}])
    save(client, "b.png", [{"x": 3, "y": 3, "w": 5, "h": 5}])
    save(client, "a.png", [{"x": 40, "y": 40, "w": 9, "h": 9}])
    assert stored(dataset) == {"a.png": [[40, 40, 9, 9]], "b.png": [[3, 3, 5, 5]]}
    ids = [a["id"] for a in coco.load_coco()["annotations"]]
    assert len(ids) == len(set(ids))


def test_empty_save_counts_as_reviewed_negative(client, make_image):
    make_image("a.png")
    make_image("b.png")
    save(client, "b.png", [])
    listing = client.get("/api/images").json()
    assert listing["annotated"] == 1
    assert listing["images"][1] == {"file_name": "b.png", "annotated": True, "box_count": 0}


@pytest.mark.parametrize("name", ["..%2Fa.png", "..%5Ca.png", "d.tif", "missing.png"])
def test_rejects_paths_and_unsupported_files(client, make_image, name):
    make_image("a.png")
    make_image("d.tif")
    assert client.get(f"/api/images/{name}").status_code == 404
    assert save(client, name, []).status_code == 404


def test_invalid_box_is_rejected(client, make_image):
    make_image("a.png")
    assert save(client, "a.png", [{"x": -1, "y": 0, "w": 5, "h": 5}]).status_code == 422
    assert save(client, "a.png", [{"x": 0, "y": 0, "w": 0, "h": 5}]).status_code == 422


def test_writes_are_atomic(client, make_image, dataset):
    make_image("a.png")
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])
    assert not list(dataset.glob("*.tmp"))
    json.loads(coco.COCO_PATH.read_text(encoding="utf-8"))


def test_saved_file_has_one_record_per_line(client, make_image):
    make_image("a.png")
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}, {"x": 9, "y": 9, "w": 5, "h": 5}])
    lines = coco.COCO_PATH.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith('{"id": ') and '"bbox"' in line for line in lines) == 2


def test_corrupt_file_is_backed_up_not_wiped(client, make_image, dataset):
    make_image("a.png")
    for _ in range(2):                                   # two in the same second keep both backups
        coco.COCO_PATH.write_text("{broken", encoding="utf-8")
        assert client.get("/api/images").json()["annotated"] == 0
    backups = list(dataset.glob("coco_annotations.corrupt-*.json"))
    assert len(backups) == 2 and all(b.read_text(encoding="utf-8") == "{broken" for b in backups)


@pytest.mark.parametrize("content", [
    "[]", '{"images": {}}', '{"annotations": [{"id": 1, "image_id": 1}]}',
    '{"images": [{"id": "1", "file_name": "a.png"}]}',                                         # a text id
    '{"annotations": [{"id": 1, "image_id": 1, "category_id": null, "bbox": [1, 1, 5, 5]}]}',
    '{"annotations": [{"id": 1, "image_id": 1, "category_id": 1, "bbox": [1, 1, 5]}]}',
])
def test_file_the_app_cant_use_is_backed_up_too(client, make_image, dataset, content):
    make_image("a.png")
    coco.COCO_PATH.write_text(content, encoding="utf-8")
    assert client.get("/api/images").json()["annotated"] == 0
    assert len(list(dataset.glob("coco_annotations.corrupt-*.json"))) == 1


def test_a_file_locked_for_a_moment_is_not_called_corrupt(client, make_image, dataset, monkeypatch):
    """Windows: a virus scanner or another program holding the file must not make the labels 'vanish'."""
    make_image("a.png")
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])
    coco._cache.clear()
    real, attempts = Path.read_bytes, []

    def locked(times):
        def read_bytes(self):
            if self == coco.COCO_PATH and len(attempts) < times:
                attempts.append(1)
                raise PermissionError(13, "The file is being used by another process")
            return real(self)
        return read_bytes

    monkeypatch.setattr(Path, "read_bytes", locked(2))                       # busy twice, then free
    assert client.get("/api/images").json()["annotated"] == 1

    coco._cache.clear()
    attempts.clear()
    monkeypatch.setattr(Path, "read_bytes", locked(99))                      # stays locked: an error, not a wipe
    with pytest.raises(PermissionError):
        client.get("/api/images")
    monkeypatch.setattr(Path, "read_bytes", real)
    assert not list(dataset.glob("*.corrupt-*")) and stored(dataset) == {"a.png": [[1, 1, 5, 5]]}


def test_a_save_that_cant_be_written_changes_nothing(client, make_image, dataset, monkeypatch):
    for name in ("a.png", "b.png"):
        make_image(name)
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])
    real = coco.save_coco

    def disk_full(data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(coco, "save_coco", disk_full)
    with pytest.raises(OSError):
        save(client, "a.png", [])
    monkeypatch.setattr(coco, "save_coco", real)
    assert client.get("/api/annotations/a.png").json()["boxes"][0]["w"] == 5   # not shown as saved...
    save(client, "b.png", [])
    assert stored(dataset) == {"a.png": [[1, 1, 5, 5]], "b.png": []}          # ...nor written by the next save


def test_file_with_a_byte_order_mark_loads(client, make_image):
    make_image("a.png")
    seed(("a.png", 100, 80, [[1, 1, 5, 5]]))
    coco.COCO_PATH.write_bytes(b"\xef\xbb\xbf" + coco.COCO_PATH.read_bytes())   # as some Windows editors save it
    assert client.get("/api/images").json()["annotated"] == 1


def test_changes_made_outside_the_app_are_picked_up(client, make_image):
    make_image("a.png")
    assert client.get("/api/images").json()["annotated"] == 0
    data = json.loads(coco.COCO_PATH.read_text(encoding="utf-8"))
    data["images"].append({"id": 1, "file_name": "a.png", "width": 100, "height": 80})
    coco.COCO_PATH.write_text(json.dumps(data), encoding="utf-8")
    assert client.get("/api/images").json()["annotated"] == 1


def test_parallel_saves_all_land(make_image, dataset):
    names = [f"{i}.png" for i in range(40)]
    for name in names:
        make_image(name)
    body = SaveRequest(width=100, height=80, boxes=[Box(x=1, y=1, w=5, h=5, category_id=1)])
    errors = []

    def work(chunk):
        try:
            for name in chunk:
                routes.save_annotations(name, body)
                routes.list_images()                      # readers in between
        except Exception as e:                            # reported below: a thread can't fail the test itself
            errors.append(e)

    threads = [threading.Thread(target=work, args=(names[k::8],)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    coco._cache.clear()                                   # check the file itself, not memory
    assert stored(dataset) == {name: [[1, 1, 5, 5]] for name in names}
    data = coco.load_coco()
    for section in ("images", "annotations"):
        ids = [r["id"] for r in data[section]]
        assert len(ids) == len(set(ids)) == 40


@pytest.mark.parametrize("host, ok", [("127.0.0.1:8000", True), ("localhost:8000", True), ("[::1]:8000", True),
                                       ("192.168.1.10:8000", True), ("evil.example:8000", False),
                                       ("mypc:8000", True)])
def test_only_known_host_names_are_answered(dataset, monkeypatch, host, ok):
    """A page that points its own domain at 127.0.0.1 (DNS rebinding) must not read the images."""
    monkeypatch.setattr(main, "ALLOWED_HOSTS", {"mypc"})
    r = TestClient(app).get("/api/images", headers={"host": host})
    assert (r.status_code == 200) is ok


@pytest.mark.parametrize("method, path, headers, ok", [
    ("POST", "/api/categories", {"origin": "http://localhost:5173"}, False),      # another site's page
    ("POST", "/api/categories", {"origin": "null"}, False),                       # a sandboxed page or a local file
    ("POST", "/api/categories", {"sec-fetch-site": "same-site"}, False),          # another port on this computer
    ("POST", "/api/categories", {"origin": "http://127.0.0.1:9999"}, False),      # the same, from a browser without Sec-Fetch
    ("GET", "/api/coco", {"sec-fetch-site": "cross-site"}, False),
    ("POST", "/api/categories", {"origin": "http://127.0.0.1:8000", "sec-fetch-site": "same-origin"}, True),
    ("POST", "/api/categories", {}, True),                                        # a script or curl
    ("GET", "/", {"sec-fetch-site": "cross-site"}, True),                         # a link to the app from anywhere
    ("GET", "/api/images", {"sec-fetch-site": "none"}, True),                     # typed into the address bar
])
def test_other_websites_cant_use_the_api(client, method, path, headers, ok):
    """Without this, any web page could post to /api/workspace/clear through the user's browser."""
    r = client.request(method, path, json={"name": "x"} if method == "POST" else None, headers=headers)
    assert r.status_code == (403 if not ok else 201 if method == "POST" else 200)


def test_export_downloads_coco(client, make_image):
    assert client.get("/api/coco").json()["annotations"] == []
    make_image("a.png")
    save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])
    r = client.get("/api/coco")
    assert "attachment" in r.headers["content-disposition"]
    assert r.json()["annotations"][0]["bbox"] == [1, 1, 5, 5]


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Box Annotator" in r.text
    for name, kind in [("js/main.js", "text/javascript"), ("js/canvas.js", "text/javascript"), ("style.css", "text/css")]:
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and r.headers["cache-control"] == "no-cache"
    # only the UI's own files
    for name in ["index.html", "js/nope.js", "js/../index.html", "js%2F..%2F..%2Froutes.py"]:
        assert client.get(f"/static/{name}").status_code == 404, name


def test_a_new_ui_module_is_served_without_a_restart(client, tmp_path, monkeypatch):
    (tmp_path / "js").mkdir()
    (tmp_path / "js/extra.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setattr(routes, "STATIC_DIR", tmp_path)
    r = client.get("/static/js/extra.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")


def test_other_sites_cant_show_the_app_in_a_frame(client):
    """A page laid over a framed app could trick the user into clicking Clear (clickjacking)."""
    for r in [client.get("/"), client.get("/api/images"), client.get("/static/js/main.js"), client.get("/nope"),
              client.post("/api/categories", json={"name": "x"}, headers={"origin": "http://evil.example"})]:
        assert r.headers["x-frame-options"] == "DENY", r.url
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'", r.url


def test_a_refused_change_keeps_the_labels_in_memory(client, make_image, monkeypatch):
    """With 200k boxes a re-read takes over a second, and a 409 for a taken class name is an everyday thing."""
    make_image("a.png")
    client.get("/api/categories")
    parses = []
    real = coco._parse
    monkeypatch.setattr(coco, "_parse", lambda raw: parses.append(1) or real(raw))
    assert client.post("/api/categories", json={"name": "Object"}).status_code == 409
    assert save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5, "category_id": 9}]).status_code == 422
    assert save(client, "gone.png", []).status_code == 404
    assert client.get("/api/categories").status_code == 200
    assert parses == []


def test_a_save_waiting_on_a_clear_doesnt_label_the_deleted_image(client, make_image, dataset):
    """The image was checked before the save waited for the lock, so it landed after the Clear."""
    make_image("a.png")
    result = {}
    with coco.LOCK:                                                      # held by a Clear while it deletes
        saving = threading.Thread(target=lambda: result.update(r=save(client, "a.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])))
        saving.start()
        time.sleep(0.3)   # the save is waiting for the lock by now (the test passes either way when the code is right)
        (dataset / "images/a.png").unlink()
    saving.join(10)
    assert result["r"].status_code == 404 and stored(dataset) == {}
