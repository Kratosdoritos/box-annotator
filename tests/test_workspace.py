"""Workspace tests: importing images and labels, exporting (download or folder) and clearing."""

import errno
import io
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import coco, config, exports, workspace
from app.main import app

from conftest import seed, stored


def png(size=(100, 80)):
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 90, 90)).save(buf, "PNG")
    return buf.getvalue()


def upload(client, name, data=None):
    return client.put(f"/api/images/{name}", content=png() if data is None else data)


def label(client, name, boxes, size=(100, 80)):
    boxes = [{"category_id": 1, **b} for b in boxes]
    return client.put(f"/api/annotations/{name}", json={"width": size[0], "height": size[1], "boxes": boxes})


# ---- importing images ----------------------------------------------------------

def test_import_image_copies_it_into_the_workspace(client, dataset):
    assert upload(client, "a.png").status_code == 201
    assert (dataset / "images/a.png").read_bytes() == png()
    assert client.get("/api/images").json()["images"] == [{"file_name": "a.png", "annotated": False, "box_count": 0}]


def test_import_never_overwrites(client, dataset):
    upload(client, "a.png")
    other = png((50, 50))
    r = upload(client, "a.png", other)
    assert r.status_code == 409 and "different image" in r.json()["detail"]
    assert (dataset / "images/a.png").read_bytes() == png()
    r = upload(client, "a.png")                                           # the very same file again is fine
    assert r.status_code == 200 and r.json() == {"file_name": "a.png", "existing": True}


def test_labels_imported_before_their_image_find_it_whatever_the_capitals(client, dataset):
    r = client.post("/api/import/coco", json={
        "images": [{"id": 1, "file_name": "IMG1.PNG", "width": 100, "height": 80}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 2, 3, 4]}],
        "categories": [{"id": 1, "name": "object"}]})
    assert r.json()["not_in_workspace"] == 1
    upload(client, "img1.png")
    assert client.get("/api/annotations/img1.png").json()["boxes"] == [{"x": 1, "y": 2, "w": 3, "h": 4, "category_id": 1}]
    assert [i["file_name"] for i in coco.load_coco()["images"]] == ["img1.png"]


def test_a_name_with_half_an_emoji_still_imports(client, dataset):
    half_emoji = '{"images": [{"id": 1, "file_name": "a.png", "width": 100, "height": 80}], "categories": [{"id": 1, "name": "x \\ud83d"}]}'
    r = client.post("/api/import/coco", content=half_emoji, headers={"content-type": "application/json"})
    assert r.status_code == 200                                            # it can't be saved as UTF-8 as it is
    assert [c["name"] for c in client.get("/api/categories").json()] == ["x \ufffd"]
    assert client.post("/api/categories", json={"name": "ok"}).status_code == 201   # and nothing is stuck


@pytest.mark.parametrize("name, data, status", [
    ("..%2Fa.png", None, 404),                    # a path: never reaches the handler
    ("..%5Ca.png", None, 400),                    # a Windows path
    ("a.tif", b"II*\x00", 415),                   # unsupported type
    ("a.png", b"hello, not a picture", 422),      # wrong content for the extension
    ("a.jpg", None, 422),                         # a PNG named .jpg
])
def test_import_rejects_bad_files(client, dataset, name, data, status):
    assert upload(client, name, data).status_code == status
    assert not list(dataset.glob("**/*.*g"))


def test_import_rejects_huge_images(client, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_IMAGE_BYTES", 100)
    assert upload(client, "a.png").status_code == 413


def test_import_stops_at_the_limit_without_a_length(client, dataset, monkeypatch):
    """A body sent in chunks has no Content-Length, so only reading it can tell how big it is."""
    monkeypatch.setattr(workspace, "MAX_IMAGE_BYTES", 1000)
    chunks = (b"\x89PNG\r\n\x1a\n" + b"x" * 92 for _ in range(100))   # 10 kB, in 100-byte chunks
    assert client.put("/api/images/a.png", content=chunks).status_code == 413
    assert not (dataset / "images/a.png").exists()


def test_a_different_picture_of_the_same_size_is_still_refused(client, dataset):
    """Same name, same byte count (e.g. two BMP scans of one size): only the bytes tell them apart."""
    def bmp(colour):
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), colour).save(buf, "BMP")
        return buf.getvalue()
    first, second = bmp((90, 90, 90)), bmp((200, 0, 0))
    assert len(first) == len(second) and first != second
    upload(client, "scan.bmp", first)
    assert upload(client, "scan.bmp", second).status_code == 409
    assert (dataset / "images/scan.bmp").read_bytes() == first


@pytest.mark.parametrize("error, status, says", [
    (OSError(errno.ENOSPC, "No space left on device"), 500, "Couldn't write a.png into the workspace: No space left on device"),
    (PermissionError(errno.EACCES, "Permission denied"), 500, "Couldn't write a.png into the workspace: Permission denied"),
    (OSError(errno.EINVAL, "Invalid argument"), 400, "Can't save a file called a.png here (Invalid argument)"),
])
def test_import_says_whether_the_disk_or_the_name_is_at_fault(client, monkeypatch, error, status, says):
    def fail(path, data):
        raise error
    monkeypatch.setattr(workspace, "_store_image", fail)
    r = upload(client, "a.png")
    assert r.status_code == status and r.json()["detail"] == says


# ---- importing labels ----------------------------------------------------------

def coco_file(**overrides):
    data = {
        "images": [{"id": 7, "file_name": "scans\\a.png", "width": 100, "height": 80},
                   {"id": 8, "file_name": "b.png", "width": 100, "height": 80},
                   {"id": 9, "file_name": "later.png", "width": 100, "height": 80}],
        "annotations": [{"image_id": 7, "category_id": 5, "bbox": [90, 70, 50, 50]},    # clipped to 10x10
                        {"image_id": 8, "category_id": 5, "bbox": [1, 1, 5, 5]},
                        {"image_id": 9, "category_id": 6, "bbox": [2, 2, 5, 5]},        # unlisted class
                        {"image_id": 7, "category_id": 4, "bbox": [200, 200, 5, 5]}],   # outside: dropped
        "categories": [{"id": 4, "name": "Bus"}, {"id": 5, "name": "car"}],
        "info": {"ignored": True},
    }
    data.update(overrides)
    return data


def test_import_labels_merges_classes_and_keeps_existing_work(client, dataset):
    for name in ["a.png", "b.png"]:
        upload(client, name)
    seed(("b.png", 100, 80, [[40, 40, 9, 9]]), classes=("bus",))    # b.png is already labelled
    r = client.post("/api/import/coco", json=coco_file())
    assert r.json() == {"images": 2, "boxes": 2, "classes_added": 2, "skipped_images": 1, "not_in_workspace": 1}
    assert [(c["id"], c["name"]) for c in coco.load_coco()["categories"]] == [(1, "bus"), (2, "car"), (3, "class 6")]
    assert stored(dataset) == {"b.png": [[40, 40, 9, 9]], "a.png": [[90, 70, 10, 10]], "later.png": [[2, 2, 5, 5]]}

    upload(client, "later.png")                       # its labels were waiting for it
    later = client.get("/api/annotations/later.png").json()["boxes"]
    assert later == [{"x": 2, "y": 2, "w": 5, "h": 5, "category_id": 3}]


def test_import_labels_rejects_files_that_are_not_coco(client):
    assert client.post("/api/import/coco", json={"annotations": []}).status_code == 422
    assert client.post("/api/import/coco", json=coco_file(images=[{"id": 1, "file_name": "a.png"}])).status_code == 422
    twice = [{"id": 7, "file_name": n, "width": 100, "height": 80} for n in ["a.png", "b.png"]]
    r = client.post("/api/import/coco", json=coco_file(images=twice))
    assert r.status_code == 422 and "more than once" in r.json()["detail"]   # boxes would land on the wrong image
    assert coco.load_coco()["images"] == []


def test_import_labels_find_their_image_whatever_the_case(client, dataset):
    upload(client, "img1.png")
    seed()
    r = client.post("/api/import/coco", json={
        "images": [{"id": 1, "file_name": "IMG1.PNG", "width": 100, "height": 80}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 1, 5, 5]}],
        "categories": [{"id": 1, "name": "object"}]})
    assert r.json()["not_in_workspace"] == 0
    assert stored(dataset) == {"img1.png": [[1, 1, 5, 5]]}


# ---- exporting -----------------------------------------------------------------

@pytest.fixture
def labelled(client):
    """a.png with one box, b.png a reviewed negative, c.png not labelled yet."""
    for name in ["a.png", "b.png", "c.png"]:
        upload(client, name)
    seed()
    label(client, "a.png", [{"x": 10, "y": 20, "w": 30, "h": 40}])
    label(client, "b.png", [])


def test_download_coco_json(client, labelled):
    r = client.get("/api/export/coco")
    assert r.status_code == 200
    assert 'filename="box-annotator-coco-' in r.headers["content-disposition"]
    data = r.json()
    assert [i["file_name"] for i in data["images"]] == ["a.png", "b.png"]
    assert [a["bbox"] for a in data["annotations"]] == [[10, 20, 30, 40]]


def test_download_coco_with_images_zip(client, labelled):
    r = client.get("/api/export/coco_images")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = z.namelist()[0].split("/")[0]
    assert root.startswith("box-annotator-coco-images-")
    assert sorted(n.split("/", 1)[1] for n in z.namelist() if not n.endswith("/")) == [
        "coco_annotations.json", "images/a.png", "images/b.png", "images/c.png"]   # unlabelled c.png too
    assert len(json.loads(z.read(f"{root}/coco_annotations.json"))["images"]) == 2


def test_download_yolo_zip(client, labelled):
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/export/yolo?val=0").content))
    root = z.namelist()[0].split("/")[0]
    assert z.read(f"{root}/labels/train/a.txt") == b"0 0.250000 0.500000 0.300000 0.500000\n"
    assert z.read(f"{root}/labels/train/b.txt") == b""
    assert f"{root}/data.yaml" in z.namelist()


def test_browser_download_is_prepared_then_fetched_once(client, labelled):
    d = client.post("/api/export/yolo/download", json={"val": 0}).json()
    assert d["name"].startswith("box-annotator-yolo-") and d["name"].endswith(".zip")
    assert client.get("/api/workspace").json()["exported"] is False      # not until the browser has it
    z = zipfile.ZipFile(io.BytesIO(client.get("/" + d["url"]).content))
    root = d["name"][:-4]
    yaml = z.read(f"{root}/data.yaml").decode("utf-8").splitlines()
    assert not any(line.startswith("path:") for line in yaml)          # works wherever it's unzipped
    assert z.getinfo(f"{root}/images/train/a.png").compress_type == zipfile.ZIP_STORED
    assert client.get("/api/workspace").json()["exported"] is True
    assert client.get("/" + d["url"]).status_code == 404                 # each url works once
    assert client.get("/api/downloads/not-a-token/x.zip").status_code == 404


def test_a_crafted_download_url_cant_reach_outside_the_downloads(client, labelled, dataset):
    """`..` as the token points at the dataset folder; serving it would also have deleted that folder afterwards."""
    assert client.get("/api/downloads/%2E%2E/coco_annotations.json").status_code == 404
    assert (dataset / "coco_annotations.json").exists()


def test_a_ranged_request_gets_the_whole_file(client, labelled):
    """Download managers fetch pieces in parallel: the first piece used to count as the export, and delete it."""
    d = client.post("/api/export/coco_images/download", json={}).json()
    r = client.get("/" + d["url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 200 and "accept-ranges" not in r.headers
    assert len(r.content) == int(r.headers["content-length"]) > 10
    assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None
    assert client.get("/api/workspace").json()["exported"] is True


def test_a_download_cut_off_half_way_doesnt_count_as_exported(server, client, labelled, dataset, caplog):
    """The browser's Cancel, or a dropped connection: Clear must still warn, and the browser's Retry still work."""
    caplog.set_level(logging.INFO, logger="annotator")
    pixels = os.urandom(3000 * 3000 * 3)                       # 27 MB: still being sent when the client hangs up
    Image.frombytes("RGB", (3000, 3000), pixels).save(dataset / "images/big.bmp")
    d = client.post("/api/export/coco_images/download", json={}).json()
    host, port = server.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port))) as s:
        s.sendall(f"GET /{d['url']} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        s.recv(65536)
    deadline = time.monotonic() + 10
    while "stopped before the end" not in caplog.text:
        assert time.monotonic() < deadline, "the server never noticed the client hung up"
        time.sleep(0.05)
    assert client.get("/api/workspace").json()["exported"] is False
    r = client.get("/" + d["url"])                              # Retry
    assert r.status_code == 200 and len(r.content) == int(r.headers["content-length"])
    assert client.get("/api/workspace").json()["exported"] is True


@pytest.mark.skipif(os.name != "posix", reason="Windows gives each user their own temp folder")
def test_prepared_downloads_are_private_to_this_user(client, labelled):
    workspace.DOWNLOADS.mkdir()
    workspace.DOWNLOADS.chmod(0o755)                            # e.g. left by an older version
    client.post("/api/export/coco/download", json={})
    assert stat.S_IMODE(workspace.DOWNLOADS.stat().st_mode) == 0o700


def test_exports_leave_out_labels_whose_image_isnt_here(client, labelled):
    """Labels can arrive before their images; an export lists only the pictures it holds."""
    client.post("/api/import/coco", json={
        "images": [{"id": 1, "file_name": "later.png", "width": 100, "height": 80}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 1, 5, 5]}],
        "categories": [{"id": 1, "name": "object"}]})
    data = client.get("/api/export/coco").json()
    assert [i["file_name"] for i in data["images"]] == ["a.png", "b.png"]
    assert [a["bbox"] for a in data["annotations"]] == [[10, 20, 30, 40]]


def test_an_export_reads_a_snapshot_that_later_saves_dont_change(client, labelled):
    snap = coco.snapshot()
    label(client, "c.png", [{"x": 1, "y": 1, "w": 5, "h": 5}])
    assert [i["file_name"] for i in snap["images"]] == ["a.png", "b.png"] and len(snap["annotations"]) == 1


def test_downloads_never_fetched_are_swept_when_the_app_opens(client, tmp_path):
    old, recent = tmp_path / "downloads/0123456789abcdef", tmp_path / "downloads/fedcba9876543210"
    for folder in (old, recent):
        folder.mkdir(parents=True)
        (folder / "export.zip").write_bytes(b"PK")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    client.get("/api/workspace")                                          # the UI asks this on every page load
    assert not old.exists() and recent.exists()


def test_yolo_export_needs_each_image_size(client, labelled):
    data = json.loads(coco.COCO_PATH.read_text(encoding="utf-8"))
    del data["images"][0]["width"]                                        # left out by some other tool
    coco.COCO_PATH.write_text(json.dumps(data), encoding="utf-8")
    r = client.post("/api/export/yolo/download", json={})
    assert r.status_code == 422 and "no image size for a.png" in r.json()["detail"]


def test_labels_saved_while_an_export_waits_are_not_counted_as_exported(client, labelled):
    d = client.post("/api/export/coco/download", json={}).json()
    label(client, "c.png", [])                                            # saved after the export was made
    client.get("/" + d["url"])
    assert client.get("/api/workspace").json()["exported"] is False
    assert client.post("/api/workspace/clear", json={}).status_code == 409


def test_yolo_export_with_nothing_labelled_explains_why(client):
    upload(client, "a.png")
    r = client.get("/api/export/yolo")
    assert r.status_code == 422 and "No saved images" in r.json()["detail"]


def test_export_to_a_folder(client, labelled, tmp_path_factory):
    dest = tmp_path_factory.mktemp("exports")
    paths = [client.post(f"/api/export/{fmt}", json={"folder": str(dest), "val": 0}).json()["path"]
             for fmt in ["coco", "coco", "coco_images", "yolo"]]
    names = sorted(p.name for p in dest.iterdir())
    assert len(names) == 4 and len(set(paths)) == 4                     # same-second exports don't collide
    coco_json = [p for p in dest.iterdir() if p.suffix == ".json"]
    assert len(coco_json) == 2 and json.loads(coco_json[0].read_text(encoding="utf-8"))["annotations"]
    [yolo_dir] = [p for p in dest.iterdir() if "yolo" in p.name]
    assert (yolo_dir / "labels/train/a.txt").exists()
    [images_dir] = [p for p in dest.iterdir() if "coco-images" in p.name]
    assert sorted(p.name for p in (images_dir / "images").iterdir()) == ["a.png", "b.png", "c.png"]


def test_failed_folder_export_leaves_nothing_behind(client, labelled, tmp_path_factory, monkeypatch):
    dest = tmp_path_factory.mktemp("exports")
    real, copied = exports.shutil.copy2, []

    def disk_fills_up(src, dst):
        if copied:
            raise OSError(28, "No space left on device")
        copied.append(real(src, dst))
    monkeypatch.setattr(exports.shutil, "copy2", disk_fills_up)
    r = client.post("/api/export/coco_images", json={"folder": str(dest)})
    assert r.status_code == 500 and r.json()["detail"] == "Couldn't write the export: No space left on device"
    assert copied and list(dest.iterdir()) == []


@pytest.mark.parametrize("folder", ["relative/path", "C:/no/such/folder/anywhere", "images", "images/sub"])
def test_export_folder_must_exist_and_be_outside_the_images(client, labelled, dataset, folder):
    if folder.startswith("images"):
        (dataset / folder).mkdir(exist_ok=True)
        folder = str(dataset / folder)
    assert client.post("/api/export/coco", json={"folder": folder}).status_code == 422


def test_folder_features_are_for_this_computer_only(dataset, tmp_path_factory):
    remote = TestClient(app, base_url="http://192.168.1.10:8000", client=("192.168.1.50", 50000))
    assert remote.get("/api/workspace").json()["local"] is False
    assert remote.post("/api/export/coco", json={"folder": str(tmp_path_factory.mktemp("x"))}).status_code == 403
    assert remote.post("/api/pick-folder", json={}).status_code == 403
    assert remote.get("/api/workspace").json()["can_clear"] is False   # with --host 0.0.0.0, anyone on the network
    assert remote.post("/api/workspace/clear", json={"force": True}).status_code == 403


def test_pick_folder_returns_the_chosen_path(client, monkeypatch):
    def fake_run(args, **kwargs):
        assert args[1] == "-c" and "askdirectory" in args[2]
        return subprocess.CompletedProcess(args, 0, stdout="C:/Users/me/Exports\n")
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    assert client.post("/api/pick-folder", json={}).json()["path"].replace("\\", "/") == "C:/Users/me/Exports"

    monkeypatch.setattr(workspace.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="\n"))
    assert client.post("/api/pick-folder", json={}).json() == {"path": None}            # cancelled

    monkeypatch.setattr(workspace.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout=""))
    assert client.post("/api/pick-folder", json={}).status_code == 501                  # no Tk here

    assert workspace._picker_open.acquire(blocking=False)                               # one already open
    try:
        assert client.post("/api/pick-folder", json={}).status_code == 409
    finally:
        workspace._picker_open.release()


# ---- clearing ------------------------------------------------------------------

def test_clear_waits_for_an_export(client, labelled, dataset):
    assert client.get("/api/workspace").json()["exported"] is False
    assert client.post("/api/workspace/clear", json={}).status_code == 409
    client.get("/api/export/coco")
    assert client.get("/api/workspace").json()["exported"] is True
    label(client, "c.png", [])                                            # a change after the export
    assert client.post("/api/workspace/clear", json={}).status_code == 409
    assert client.post("/api/workspace/clear", json={"force": True}).json() == {"deleted_images": 3, "in_use": []}


def test_clear_waits_for_a_running_export(client, labelled, dataset, monkeypatch):
    started, release = threading.Event(), threading.Event()
    real = exports.write_zip

    def slow_zip(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(exports, "write_zip", slow_zip)
    export = {}
    t = threading.Thread(target=lambda: export.update(r=client.post("/api/export/coco_images/download", json={})))
    t.start()
    assert started.wait(10)
    r = client.post("/api/workspace/clear", json={"force": True})       # would pull the images from under it
    release.set()
    t.join(10)
    assert r.status_code == 409 and "still being written" in r.json()["detail"]
    assert export["r"].status_code == 200 and len(list((dataset / "images").iterdir())) == 3
    assert client.post("/api/workspace/clear", json={"force": True}).status_code == 200   # once it's done


def test_clear_frees_the_space_and_can_keep_classes(client, labelled, dataset):
    (dataset / "images/.gitkeep").write_text("")
    (dataset / "images/half.png.part").write_bytes(b"\x89PNG")
    client.get("/api/export/coco")
    assert client.post("/api/workspace/clear", json={"keep_classes": True}).json() == {"deleted_images": 3, "in_use": []}
    assert sorted(p.name for p in (dataset / "images").iterdir()) == [".gitkeep"]
    assert coco.load_coco() == {**coco.empty_coco(), "categories": [{"id": 1, "name": "object", "supercategory": ""}]}
    assert client.get("/api/workspace").json() | {"images_dir": None} == {
        "images_dir": None, "images": 0, "labelled": 0, "exported": True, "set_aside": [], "can_clear": True,
        "local": True}

    upload(client, "d.png")
    client.post("/api/workspace/clear", json={"keep_classes": False})
    assert coco.load_coco() == coco.empty_coco()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete an open file")
def test_clear_keeps_an_image_another_program_has_open(client, labelled, dataset):
    with open(dataset / "images/b.png", "rb"):   # e.g. still being sent to the browser; Windows can't delete it
        r = client.post("/api/workspace/clear", json={"force": True})
    assert r.status_code == 200 and r.json() == {"deleted_images": 2, "in_use": ["b.png"]}
    assert sorted(p.name for p in (dataset / "images").iterdir()) == ["b.png"]
    assert coco.load_coco()["images"] == []                              # the labels are cleared all the same


def test_clear_is_off_for_the_users_own_folder(client, labelled, dataset, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_WORKSPACE", False)
    r = client.post("/api/workspace/clear", json={"force": True})
    assert r.status_code == 403 and "your own folder" in r.json()["detail"]
    assert len(list((dataset / "images").iterdir())) == 3


def test_a_labels_file_set_aside_is_reported_and_clear_asks_first(client, labelled, dataset):
    coco.COCO_PATH.write_text("{ not json", encoding="utf-8")   # e.g. edited by hand
    status = client.get("/api/workspace").json()
    [backup] = status["set_aside"]
    assert Path(backup).name.startswith("coco_annotations.corrupt-") and status["exported"] is False
    r = client.post("/api/workspace/clear", json={})
    assert r.status_code == 409 and "set aside" in r.json()["detail"]
    assert client.post("/api/workspace/clear", json={"force": True}).status_code == 200
    assert Path(backup).exists()                                 # Clear deletes images, never the user's labels file
