"""
Browser tests: drive the real UI in headless Chromium via Playwright.

Skipped when Playwright or a Chromium-based browser isn't available
(`pip install playwright && playwright install chromium`), except on CI,
where a browser that won't start fails the run instead of skipping it. A
failing test leaves a screenshot in test-results/.

  ANNOTATOR_TEST_BROWSER=<path to a Chromium-based browser, e.g. brave.exe>  run in that browser
  ANNOTATOR_TEST_HEADED=1                                                     show the window, slowed down
"""

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app import coco, yolo

from conftest import seed, stored

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    exe = os.environ.get("ANNOTATOR_TEST_BROWSER")
    headed = os.environ.get("ANNOTATOR_TEST_HEADED") == "1"
    view = {"headless": not headed, "slow_mo": 120 if headed else 0}
    with sync_api.sync_playwright() as p:
        if exe:   # asked for a specific browser: fail loudly rather than skip
            b = p.chromium.launch(executable_path=exe, **view)
        else:
            tried = []
            for opts in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
                try:
                    b = p.chromium.launch(**opts, **view)
                    break
                except Exception as e:
                    tried.append(f"{opts or 'playwright chromium'}: {str(e).splitlines()[0]}")
            else:
                why = "no Chromium-based browser available for Playwright: " + "; ".join(tried)
                if os.environ.get("CI"):   # on CI, skipping all browser tests must not pass as green
                    pytest.fail(why)
                pytest.skip(why)
        yield b
        b.close()


class UI:
    """Small helper around the annotator page."""

    def __init__(self, page, base):
        self.page, self.base = page, base

    def open(self):
        self.page.goto(self.base + "/")
        # started: the list is built and the first image is on screen (or, with no images, Import opened);
        # before the list arrives the canvas isn't marked as loading yet, so wait_ready alone can pass too early
        self.page.wait_for_function("""() => {
            const side = document.getElementById('side');
            if (!side.children.length) return false;
            if (side.querySelector('.empty')) return document.getElementById('importDlg').open;
            return document.getElementById('fname').textContent !== '—'
                && !document.getElementById('cv').classList.contains('loading');
        }""")

    def wait_ready(self):
        self.page.wait_for_function("!document.getElementById('cv').classList.contains('loading')")

    def wait_for(self, name):
        self.page.wait_for_function(f"document.getElementById('fname').textContent==={name!r}")
        self.wait_ready()

    def text(self, sel):
        return self.page.locator(sel).inner_text()

    @property
    def fname(self):
        return self.text("#fname")

    @property
    def box_count(self):
        return self.text("#boxcount")

    def canvas(self):
        return self.page.locator("#cv").bounding_box()

    def drag(self, x0, y0, x1, y1):
        """Drag between two points given in canvas CSS px (may lie outside it)."""
        b, m = self.canvas(), self.page.mouse
        m.move(b["x"] + x0, b["y"] + y0)
        m.down()
        m.move(b["x"] + x1, b["y"] + y1, steps=5)
        m.up()

    def click(self, x, y):
        b = self.canvas()
        self.page.mouse.click(b["x"] + x, b["y"] + y)

    def until(self, done, timeout=10):
        """Wait for something seen from Python, e.g. a dialog the page opened, while Playwright keeps delivering events."""
        deadline = time.monotonic() + timeout
        while not done():
            assert time.monotonic() < deadline, "timed out"
            self.page.wait_for_timeout(20)

    def box(self, i=0):
        return self.page.evaluate(f"(b => b && [b.x, b.y, b.w, b.h])(annotator.state.boxes[{i}])")


@pytest.fixture(autouse=True)
def one_class(dataset):
    """Start every test with one class, "object", so boxes can be drawn."""
    seed()


@pytest.fixture
def ui(browser, server, request):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    ui = UI(page, server)
    ui.errors = errors                       # a test may drop the ones it expects
    yield ui
    if getattr(request.node, "report_call", None) and request.node.report_call.failed and not page.is_closed():
        page.screenshot(path=Path("test-results") / f"{request.node.name}.png")   # what the page looked like
    ctx.close()
    assert not errors, errors


def test_resumes_at_first_unannotated_image(ui, make_image):
    for n in ["a.png", "b.png", "c.png"]:
        make_image(n)
    seed(("a.png", 100, 80, [[1, 1, 5, 5]]), ("b.png", 100, 80, []))
    ui.open()
    assert ui.fname == "c.png"
    assert ui.text("#progress") == "2 / 3 annotated"
    marks = ui.page.locator(".side .done").all_inner_texts()
    assert marks == ["✔ 1", "✔ 0", ""]


def test_draw_is_clamped_and_save_advances_once(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    make_image("b.png")
    make_image("c.png")
    ui.open()
    w, h = ui.canvas()["width"], ui.canvas()["height"]
    ui.drag(200, 150, w + 300, h + 300)          # drag far past the bottom-right corner
    ui.click(20, 20)                             # a stray click must not add a box
    assert ui.box_count == "1 box"

    saves = []
    ui.page.on("request", lambda r: r.method == "PUT" and "/api/annotations/" in r.url and saves.append(r.url))
    ui.page.locator("#save").dblclick()          # double-click must not save twice and skip an image
    ui.wait_for("b.png")                         # the click handlers sent any second save long before this
    assert len(saves) == 1 and ui.fname == "b.png"
    [[x, y, bw, bh]] = stored(dataset)["a.png"]
    assert abs(x - 200) < 2 and abs(y - 150) < 2
    assert x + bw <= 400 and y + bh <= 300
    assert ui.page.locator(".side .done").first.inner_text() == "✔ 1"


def test_unsaved_changes_prompt_before_leaving(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    make_image("b.png")
    ui.open()
    ui.drag(50, 50, 150, 150)

    ui.page.once("dialog", lambda d: d.dismiss())
    ui.page.click("#next")
    assert ui.fname == "a.png" and ui.box_count == "1 box"

    ui.page.once("dialog", lambda d: d.accept())
    ui.page.click("#next")
    ui.wait_for("b.png")
    assert stored(dataset) == {}


def test_large_image_is_scaled_and_moves_stay_inside(ui, make_image, dataset):
    make_image("big.png", (1600, 1200))
    ui.open()
    b = ui.canvas()
    sc = b["width"] / 1600
    assert sc < 1
    ui.drag(100 * sc, 100 * sc, 300 * sc, 250 * sc)          # a 200x150 box in image px
    ui.click(200 * sc, 175 * sc)                             # pick it up...
    ui.drag(200 * sc, 175 * sc, -200, -50)                   # ...and drag it off the top-left edge
    ui.page.click("#save")
    ui.page.wait_for_function("document.querySelector('.side .done').textContent==='✔ 1'")
    [[x, y, w, h]] = stored(dataset)["big.png"]
    assert (x, y) == (0, 0) and abs(w - 200) < 3 and abs(h - 150) < 3


def test_a_box_drawn_inside_another_leaves_it_alone(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    ui.open()
    ui.drag(20, 20, 380, 280)                                # a big box
    ui.drag(100, 100, 160, 150)                              # a small one inside it, straight after
    ui.click(150, 140)                                       # picks the small one, not the big one
    ui.drag(150, 140, 170, 160)                              # so this moves the small one
    ui.page.click("#save")
    ui.page.wait_for_function("document.querySelector('.side .done').textContent==='✔ 2'")
    assert sorted(stored(dataset)["a.png"]) == [[20, 20, 360, 260], [120, 120, 60, 50]]


def test_an_image_the_browser_cant_show_doesnt_keep_the_last_one(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    (dataset / "images/b.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a picture")
    seed(("a.png", 400, 300, [[10, 10, 50, 50]]), ("b.png", 400, 300, []))   # all saved: opens at a.png
    ui.open()
    assert ui.fname == "a.png" and ui.box_count == "1 box"
    ui.page.click("#next")
    ui.page.wait_for_function("document.getElementById('fname').textContent.includes('can’t be shown')")
    assert ui.box_count == "—" and ui.canvas()["width"] == 0
    assert ui.page.locator("#save").is_disabled()


def test_zoom_and_pan_still_draw_in_image_pixels(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    ui.open()
    b = ui.canvas()
    ui.page.mouse.move(b["x"] + 100, b["y"] + 100)
    ui.page.mouse.wheel(0, -300)                              # zoom in around (100, 100)
    ui.page.wait_for_function("annotator.view.zoom > 1")
    z = ui.page.evaluate("annotator.view.zoom")
    assert ui.text("#zoom") == f"{round(z * 100)}%"
    ui.drag(100, 100, 200, 160)                               # the point under the mouse stayed put
    pan = "[annotator.view.panX, annotator.view.panY]"
    x0, y0 = ui.page.evaluate(pan)
    ui.page.focus("#save")                                    # Space must pan, not press this button
    ui.page.keyboard.down("Space")
    ui.drag(300, 200, 200, 150)                               # pan: the view moves, no box is drawn
    ui.page.keyboard.up("Space")
    assert ui.page.evaluate(pan) == pytest.approx([x0 + 100 / z, y0 + 50 / z], abs=0.5 / z)
    assert ui.box_count == "1 box" and stored(dataset) == {}
    ui.page.mouse.wheel(0, 2000)                              # zooming out stops at the whole image
    ui.page.wait_for_function("annotator.view.zoom === 1")
    assert ui.text("#zoom") == "100%"
    ui.page.click("#save")
    ui.page.wait_for_function("document.querySelector('.side .done').textContent==='✔ 1'")
    [[x, y, w, h]] = stored(dataset)["a.png"]
    assert (x, y, w, h) == pytest.approx((100, 100, 100 / z, 60 / z), abs=0.6)


def test_undo_and_redo(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    make_image("b.png", (400, 300))
    ui.open()
    assert ui.page.locator("#undo").is_disabled()
    ui.drag(20, 20, 80, 80)
    ui.drag(200, 100, 260, 160)
    ui.click(230, 130)
    ui.drag(230, 130, 250, 150)                               # move the second box
    ui.page.keyboard.press("Delete")                          # then delete it
    assert ui.box_count == "1 box"
    second = "(b => b.length > 1 ? [b[1].x, b[1].y] : null)(annotator.state.boxes)"
    ui.page.keyboard.press("Control+z")
    assert ui.box_count == "2 boxes" and ui.page.evaluate(second) == [220, 120]   # back, where it was moved to
    ui.page.keyboard.press("Control+z")
    assert ui.page.evaluate(second) == [200, 100]                                # before the move
    ui.page.keyboard.press("Control+y")
    assert ui.page.evaluate(second) == [220, 120]                                # the move again
    for _ in range(3):
        ui.page.click("#undo")
    assert ui.box_count == "0 boxes" and ui.page.locator("#undo").is_disabled()
    ui.page.keyboard.press("ArrowRight")                      # undone to the saved state: nothing to ask
    ui.wait_for("b.png")


def test_arrow_keys_switch_images_but_ask_about_unsaved_boxes(ui, make_image):
    for name in ("a.png", "b.png"):
        make_image(name, (400, 300))
    ui.open()
    ui.page.keyboard.press("ArrowRight")
    ui.wait_for("b.png")
    ui.page.keyboard.press("ArrowLeft")
    ui.wait_for("a.png")
    ui.drag(20, 20, 80, 80)
    asked = []
    ui.page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))   # "Discard them?" -> no
    ui.page.keyboard.press("ArrowRight")
    ui.until(lambda: asked)
    assert ui.fname == "a.png" and ui.box_count == "1 box"


def test_delete_key_removes_selected_box(ui, make_image):
    make_image("a.png", (400, 300))
    seed(("a.png", 400, 300, [[100, 100, 100, 100]]))
    make_image("b.png")                                      # unannotated -> opened first
    ui.open()
    ui.page.locator(".side .it").first.click()
    ui.wait_for("a.png")
    assert ui.box_count == "1 box"
    ui.click(150, 150)
    ui.page.keyboard.press("Delete")
    assert ui.box_count == "0 boxes"


def test_stale_response_does_not_leak_onto_another_image(ui, make_image):
    for n in ["a.png", "b.png", "c.png"]:
        make_image(n)
    seed(("a.png", 100, 80, [[10, 10, 20, 20]]))
    ui.open()
    assert ui.fname == "b.png"
    # make c.png's annotations arrive late, after the user has moved on to a.png
    ui.page.evaluate("""() => { const real = window.fetch; window.fetch = (u, o) =>
        String(u).includes('annotations/c.png') ? new Promise(r => setTimeout(r, 800)).then(() => real(u, o)) : real(u, o); }""")
    ui.page.locator(".side .it").nth(2).click()
    ui.page.locator(".side .it").nth(0).click()
    time.sleep(1.2)
    ui.wait_ready()
    assert ui.fname == "a.png" and ui.box_count == "1 box"


def test_add_classes_and_label_boxes_with_them(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    make_image("b.png")
    seed(classes=())                                         # brand-new dataset: no classes yet
    ui.open()
    ui.drag(50, 50, 150, 150)                                # drawing needs a class first
    assert ui.box_count == "0 boxes" and "Add a class first" in ui.text("#toast")

    for n, name in enumerate(["car", "bus"], start=1):
        ui.page.fill("#catname", name)
        ui.page.press("#catname", "Enter")
        ui.page.wait_for_function(f"document.querySelectorAll('.cat').length==={n}")
    assert ui.page.locator(".cat.active .cn").inner_text() == "bus"   # the newest class is picked

    ui.drag(50, 50, 150, 150)                                # a bus...
    ui.page.keyboard.press("1")                              # ...re-classed as a car while selected
    ui.drag(200, 50, 300, 150)                               # the next box is a car too
    ui.page.keyboard.press("2")                              # and this one becomes a bus
    ui.page.fill("#catname", "x")
    ui.page.press("#catname", "Backspace")                   # typing must not delete the selected box
    assert ui.box_count == "2 boxes"

    ui.page.click("#save")
    ui.wait_for("b.png")
    data = coco.load_coco()
    assert [c["name"] for c in data["categories"]] == ["car", "bus"]
    assert [a["category_id"] for a in data["annotations"]] == [1, 2]
    assert ui.page.locator(".cat .cc").all_inner_texts() == ["1", "1"]


def test_rename_and_delete_classes(ui, make_image):
    make_image("a.png", (400, 300))
    seed(("a.png", 400, 300, [[10, 10, 50, 50, 1]]), classes=("car", "bus"))
    ui.open()
    ui.page.once("dialog", lambda d: d.accept("truck"))
    ui.page.locator(".cat").nth(0).locator("button[title='Rename']").click()
    ui.page.wait_for_function("document.querySelector('.cat .cn').textContent==='truck'")

    ui.page.locator(".cat").nth(0).locator("button[title^='Delete']").click()   # in use: refused
    assert "used on this image" in ui.text("#toast")
    ui.page.locator(".cat").nth(1).locator("button[title^='Delete']").click()   # unused: deleted
    ui.page.wait_for_function("document.querySelectorAll('.cat').length===1")
    assert coco.load_coco()["categories"] == [{"id": 1, "name": "truck", "supercategory": ""}]


def test_full_workflow(ui, make_image, dataset):
    """One pass through everything a user does, ending in a COCO download and a YOLO export."""
    for name in ["a.png", "b.png", "c.png"]:
        make_image(name, (400, 300))                         # small enough to show at 1:1
    seed(classes=())
    ui.open()
    assert ui.fname == "a.png" and ui.text("#progress") == "0 / 3 annotated"

    for n, name in enumerate(["enamel", "dentin", "deep"], start=1):
        ui.page.fill("#catname", name)
        ui.page.press("#catname", "Enter")
        ui.page.wait_for_function(f"document.querySelectorAll('.cat').length==={n}")
    ui.page.once("dialog", lambda d: d.accept("deep caries"))
    ui.page.locator(".cat").nth(2).locator("button[title='Rename']").click()
    ui.page.wait_for_function("document.querySelectorAll('.cat .cn')[2].textContent==='deep caries'")

    # a.png: two boxes in different classes, one resized, one moved, a third drawn then deleted
    ui.page.keyboard.press("1")
    ui.drag(20, 20, 80, 80)                                  # enamel
    ui.drag(200, 100, 260, 160)                              # enamel...
    ui.page.keyboard.press("2")                              # ...re-classed as dentin
    ui.drag(260, 160, 300, 200)                              # resize it by its bottom-right handle
    ui.click(50, 50)
    ui.drag(50, 50, 60, 60)                                  # move the first box by (10, 10)
    ui.drag(320, 220, 360, 260)
    ui.page.click("#del")
    assert ui.box_count == "2 boxes"
    ui.page.click("#save")
    ui.wait_for("b.png")

    ui.page.click("#save")                                   # b.png: nothing to label
    ui.wait_for("c.png")

    ui.drag(50, 50, 100, 100)
    ui.page.click("#clear")
    assert ui.box_count == "0 boxes"
    ui.page.keyboard.press("3")
    ui.drag(100, 100, 150, 150)                              # deep caries
    ui.page.click("#save")                                   # last image: stays put
    ui.page.wait_for_function("document.getElementById('progress').textContent==='3 / 3 annotated'")
    assert ui.fname == "c.png"

    ui.page.click("#prev")
    ui.wait_for("b.png")
    ui.page.click("#prev")
    ui.wait_for("a.png")
    assert ui.box_count == "2 boxes"                          # saved boxes reload
    assert ui.page.locator(".side .done").all_inner_texts() == ["✔ 2", "✔ 0", "✔ 1"]
    assert ui.page.locator(".cat .cc").all_inner_texts() == ["1", "1", "1"]

    ui.page.click("#exportBtn")
    with ui.page.expect_download() as dl:
        ui.page.click("#doExport")                           # COCO JSON + Download are the defaults
    assert dl.value.suggested_filename.startswith("box-annotator-coco-")
    ui.page.click("#exportClose")
    data = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))
    assert [c["name"] for c in data["categories"]] == ["enamel", "dentin", "deep caries"]
    by_image = {img["file_name"]: [(a["category_id"], a["bbox"]) for a in data["annotations"] if a["image_id"] == img["id"]]
                for img in data["images"]}
    assert by_image.keys() == {"a.png", "b.png", "c.png"} and by_image["b.png"] == []
    (c1, b1), (c2, b2) = by_image["a.png"]
    [(c3, b3)] = by_image["c.png"]
    assert (c1, c2, c3) == (1, 2, 3)
    for got, want in [(b1, [30, 30, 60, 60]), (b2, [200, 100, 100, 100]), (b3, [100, 100, 50, 50])]:
        assert all(abs(g - w) <= 1 for g, w in zip(got, want, strict=True)), (got, want)

    # FastAPI's docs page loads Swagger UI from a CDN: answer those here, so the test doesn't need the internet
    ui.page.context.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(body="", content_type="text/plain"))
    with ui.page.context.expect_page() as docs:
        ui.page.click("text=API docs")
    docs.value.wait_for_load_state()
    assert docs.value.title() == "Box Annotator - Swagger UI"
    docs.value.close()

    assert yolo.export_yolo(dataset / "yolo", val_fraction=0) == {"train": 3, "val": 0, "boxes": 3}
    a_labels = (dataset / "yolo/labels/train/a.txt").read_text(encoding="utf-8").splitlines()
    assert [row.split()[0] for row in a_labels] == ["0", "1"]


def test_start_by_importing_a_folder(ui, dataset, tmp_path_factory):
    src = tmp_path_factory.mktemp("my_xrays")
    Image.new("RGB", (400, 300), (90, 90, 90)).save(src / "a.png")
    Image.new("RGB", (300, 200), (90, 90, 90)).save(src / "b.jpg")
    (src / "notes.txt").write_text("not an image", encoding="utf-8")
    (src / "coco_annotations.json").write_text(json.dumps({
        "images": [{"id": 1, "file_name": "a.png", "width": 400, "height": 300}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [10, 10, 50, 50]}],
        "categories": [{"id": 1, "name": "caries"}]}), encoding="utf-8")
    seed(classes=())

    ui.page.goto(ui.base + "/")
    ui.page.wait_for_selector("#importDlg[open]")             # an empty workspace starts here
    ui.page.set_input_files("#dirInput", str(src))
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.includes('Labels from')")
    status = ui.text("#importStatus")
    assert "Imported 2 image(s)." in status and "1 other file(s) ignored" in status
    assert "Labels from coco_annotations.json: 1 image(s), 1 box(es), 1 new class(es)" in status
    ui.wait_for("b.jpg")                                      # a.png arrived labelled
    assert ui.page.locator(".side .done").all_inner_texts() == ["✔ 1", ""]
    assert ui.page.locator(".cat .cn").all_inner_texts() == ["caries"]
    assert sorted(p.name for p in (dataset / "images").iterdir()) == ["a.png", "b.jpg"]


def test_labels_never_land_on_a_different_picture_with_the_same_name(ui, make_image, dataset, tmp_path_factory):
    make_image("R1.png", (100, 80))                                       # in the workspace, not labelled yet

    def batch(folder, images, boxes):
        src = tmp_path_factory.mktemp(folder)
        for name, (size, colour) in images.items():
            Image.new("RGB", size, colour).save(src / name)
        (src / "labels.json").write_text(json.dumps({
            "images": [{"id": i, "file_name": n, "width": s[0], "height": s[1]} for i, (n, (s, _)) in enumerate(images.items())],
            "annotations": [{"image_id": i, "category_id": 1, "bbox": b} for i, b in enumerate(boxes)],
            "categories": [{"id": 1, "name": "object"}]}), encoding="utf-8")
        return str(src)

    ui.open()
    ui.page.click("#importBtn")
    # another camera's R1.png: its box must not end up on ours
    ui.page.set_input_files("#dirInput", batch("other", {"R1.png": ((400, 300), (200, 0, 0)), "R2.png": ((400, 300), (0, 0, 200))},
                                                [[300, 200, 90, 90], [10, 10, 50, 50]]))
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.includes('Labels from')")
    status = ui.text("#importStatus")
    assert "Imported 1 image(s), skipped 1." in status and "labels for 1 image(s) not used" in status
    assert stored(dataset) == {"R2.png": [[10, 10, 50, 50]]}
    assert ui.errors == ["Failed to load resource: the server responded with a status of 409 (Conflict)"]
    ui.errors.clear()                                                     # the browser logs the refused upload
    # the very same R1.png again, now with its labels: those do belong to it
    ui.page.set_input_files("#dirInput", batch("same", {"R1.png": ((100, 80), (90, 90, 90))}, [[1, 1, 5, 5]]))
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.includes('1 already here')")
    assert stored(dataset) == {"R2.png": [[10, 10, 50, 50]], "R1.png": [[1, 1, 5, 5]]}


def test_labels_imported_for_the_image_on_screen_show_up(ui, make_image, dataset, tmp_path_factory):
    make_image("a.png", (400, 300))
    ui.open()
    assert ui.fname == "a.png" and ui.box_count == "0 boxes"
    labels = tmp_path_factory.mktemp("labels") / "labels.json"
    labels.write_text(json.dumps({
        "images": [{"id": 1, "file_name": "a.png", "width": 400, "height": 300}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [10, 10, 50, 50]}],
        "categories": [{"id": 1, "name": "object"}]}), encoding="utf-8")
    ui.page.click("#importBtn")
    ui.page.set_input_files("#labelInput", str(labels))
    ui.page.wait_for_function("document.getElementById('boxcount').textContent==='1 box'")
    ui.page.click("#importClose")
    ui.page.click("#save")                                    # must keep the imported box, not save an empty image
    ui.page.wait_for_function("document.querySelector('.side .done').textContent==='✔ 1'")
    assert stored(dataset) == {"a.png": [[10, 10, 50, 50]]}


def test_big_folder_loads_and_saves_without_rebuilding_the_list(ui, dataset):
    (dataset / "images").mkdir()
    png = io.BytesIO()
    Image.new("RGB", (400, 300), (90, 90, 90)).save(png, "PNG")
    for i in range(3000):
        (dataset / "images" / f"img{i}.png").write_bytes(png.getvalue())
    ui.open()
    assert ui.fname == "img0.png" and ui.page.locator(".side .it").count() == 3000
    assert ui.text("#progress") == "0 / 3000 annotated"
    ui.page.evaluate("document.querySelector('.side .it').dataset.probe = 'kept'")
    ui.drag(50, 50, 150, 150)
    ui.page.click("#save")
    ui.wait_for("img1.png")
    assert ui.text(".side .it[data-probe=kept] .done") == "✔ 1"   # the same row, updated in place
    assert ui.text("#progress") == "1 / 3000 annotated"
    ui.page.locator(".side .it").nth(2999).click()                 # the far end of the list works too
    ui.wait_for("img2999.png")


def test_export_window_shows_only_what_applies(ui, make_image):
    make_image("a.png")
    ui.open()
    ui.page.click("#exportBtn")
    assert ui.page.locator("#valRow").is_hidden()             # validation share is YOLO only
    ui.page.check("input[name=fmt][value=yolo]")
    assert ui.page.locator("#valRow").is_visible()
    assert ui.page.locator("#folderOpt").is_visible()         # this computer: folders are offered


def test_drop_images_onto_the_page(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    ui.open()
    ui.page.evaluate("""async () => {
        const c = document.createElement('canvas'); c.width = 64; c.height = 48;
        const blob = await new Promise(r => c.toBlob(r, 'image/png'));
        const dt = new DataTransfer();
        dt.items.add(new File([blob], 'dropped.png', {type: 'image/png'}));
        document.body.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
    }""")
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.startsWith('Imported 1 image')")
    assert (dataset / "images/dropped.png").exists()


def test_a_second_import_waits_for_the_first(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    ui.open()
    ui.page.evaluate("""async () => {
        const status = document.getElementById('importStatus');
        window.seen = [];
        new MutationObserver(() => window.seen.push(status.textContent)).observe(status, {childList: true});
        const c = document.createElement('canvas'); c.width = 64; c.height = 48;
        const blob = await new Promise(r => c.toBlob(r, 'image/png'));
        for (const [prefix, n] of [['first', 12], ['second', 8]]) {   // two drops in a row
            const dt = new DataTransfer();
            for (let i = 0; i < n; i++) dt.items.add(new File([blob], prefix + i + '.png', {type: 'image/png'}));
            document.body.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
        }
    }""")
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.startsWith('Imported 8 image')")
    totals = [s.split(" / ")[1] for s in ui.page.evaluate("window.seen") if s.startswith("Importing")]
    assert "8…" in totals and "12…" not in totals[totals.index("8…"):]   # one progress bar at a time
    assert len(list((dataset / "images").iterdir())) == 21


def test_export_to_a_folder_then_clear(ui, make_image, dataset, tmp_path_factory):
    make_image("a.png", (400, 300))
    make_image("b.png", (400, 300))
    seed(("a.png", 400, 300, [[10, 10, 50, 50]]), ("b.png", 400, 300, []), classes=("caries",))
    ui.open()
    dest = tmp_path_factory.mktemp("exports")
    ui.page.click("#exportBtn")
    ui.page.check("input[name=fmt][value=yolo]")
    ui.page.fill("#val", "0")
    ui.page.fill("#folder", str(dest))                        # typing a folder picks "a folder on this computer"
    ui.page.click("#doExport")
    ui.page.wait_for_function("document.getElementById('exportStatus').textContent.startsWith('Saved to')")
    [out] = dest.iterdir()
    assert (out / "labels/train/a.txt").exists() and (out / "data.yaml").exists()

    ui.page.once("dialog", lambda d: d.accept())              # "Delete the 2 image(s)...?"
    ui.page.click("#clearWs")
    ui.page.wait_for_selector("#importDlg[open]")             # empty again: back to importing
    assert list((dataset / "images").iterdir()) == []
    assert [c["name"] for c in coco.load_coco()["categories"]] == ["caries"]   # classes kept
    assert (out / "labels/train/a.txt").exists()              # the export is untouched


def test_regrade_an_imported_dataset_and_export_it(ui, dataset, tmp_path_factory):
    """Import a labelled dataset, rename its class, move a box to a new class, export, and import the export again."""
    src = tmp_path_factory.mktemp("old_dataset")
    for name in ("x1.png", "x2.png"):
        Image.new("RGB", (400, 300), (90, 90, 90)).save(src / name)
    (src / "coco_annotations.json").write_text(json.dumps({
        "images": [{"id": 1, "file_name": "x1.png", "width": 400, "height": 300},
                   {"id": 2, "file_name": "x2.png", "width": 400, "height": 300}],
        "annotations": [{"image_id": 1, "category_id": 1, "bbox": [20, 20, 60, 60]},
                        {"image_id": 1, "category_id": 1, "bbox": [200, 100, 60, 60]},
                        {"image_id": 2, "category_id": 1, "bbox": [50, 50, 40, 40]}],
        "categories": [{"id": 1, "name": "cavity"}]}), encoding="utf-8")
    seed(classes=())
    ui.page.goto(ui.base + "/")
    ui.page.wait_for_selector("#importDlg[open]")
    ui.page.set_input_files("#dirInput", str(src))
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.includes('Labels from')")
    ui.page.click("#importClose")
    ui.wait_for("x1.png")

    ui.page.once("dialog", lambda d: d.accept("enamel caries"))
    ui.page.locator(".cat").nth(0).locator("button[title='Rename']").click()
    ui.page.wait_for_function("document.querySelector('.cat .cn').textContent==='enamel caries'")
    ui.page.fill("#catname", "dentin caries")
    ui.page.press("#catname", "Enter")
    ui.page.wait_for_function("document.querySelectorAll('.cat').length===2")
    ui.click(230, 130)                                        # the second box on x1.png...
    ui.page.keyboard.press("2")                               # ...is dentin caries
    ui.page.click("#save")
    ui.wait_for("x2.png")
    assert ui.page.locator(".cat .cn").all_inner_texts() == ["enamel caries", "dentin caries"]
    assert ui.page.locator(".cat .cc").all_inner_texts() == ["2", "1"]

    def download(fmt):
        ui.page.check(f"input[name=fmt][value={fmt}]")
        with ui.page.expect_download() as dl:
            ui.page.click("#doExport")
        return zipfile.ZipFile(dl.value.path())

    ui.page.click("#exportBtn")
    out = tmp_path_factory.mktemp("unzipped")
    with download("coco_images") as z:
        z.extractall(out)
    [folder] = out.iterdir()
    exported = json.loads((folder / "coco_annotations.json").read_text(encoding="utf-8"))
    assert exported["categories"] == [{"id": 1, "name": "enamel caries", "supercategory": ""},
                                      {"id": 2, "name": "dentin caries", "supercategory": ""}]
    with download("yolo") as z:
        root = z.namelist()[0].split("/")[0]
        assert 'names:\n  0: "enamel caries"\n  1: "dentin caries"\n' in z.read(f"{root}/data.yaml").decode()
        x1 = next(n for n in z.namelist() if n.endswith("/x1.txt"))
        assert sorted(line.split()[0] for line in z.read(x1).decode().splitlines()) == ["0", "1"]

    # carry on somewhere else: an empty workspace with no classes, then import the export
    ui.page.uncheck("#keepCls")
    ui.page.on("dialog", lambda d: d.accept())
    ui.page.click("#clearWs")
    ui.page.wait_for_selector("#importDlg[open]")
    assert coco.load_coco() == coco.empty_coco()
    ui.page.set_input_files("#dirInput", str(folder))
    ui.page.wait_for_function("document.getElementById('importStatus').textContent.includes('Labels from')")
    data = coco.load_coco()
    names = {c["id"]: c["name"] for c in data["categories"]}
    files = {i["id"]: i["file_name"] for i in data["images"]}
    assert sorted((files[a["image_id"]], names[a["category_id"]], a["bbox"]) for a in data["annotations"]) == [
        ("x1.png", "dentin caries", [200, 100, 60, 60]),
        ("x1.png", "enamel caries", [20, 20, 60, 60]),
        ("x2.png", "enamel caries", [50, 50, 40, 40])]


def test_clear_warns_about_labels_not_exported(ui, make_image, dataset):
    make_image("a.png", (400, 300))
    seed(("a.png", 400, 300, [[10, 10, 50, 50]]))
    ui.open()
    ui.page.click("#exportBtn")
    asked = []
    ui.page.on("dialog", lambda d: (asked.append(d.message), d.accept() if len(asked) == 1 else d.dismiss()))
    clears = []
    ui.page.on("request", lambda r: "/api/workspace/clear" in r.url and clears.append(r))
    ui.page.click("#clearWs")
    ui.until(lambda: len(asked) == 2)
    assert "since your last export" in asked[1]
    ui.page.evaluate("0")                        # runs after the handler went on past the "no"
    assert not clears
    assert (dataset / "images/a.png").exists()


# ---- fixes from the review -------------------------------------------------------

def test_undo_cant_bring_back_a_box_of_a_deleted_class(ui, make_image):
    """A class added after the delete gets the old id: an undone box would have been saved under it."""
    make_image("a.png", (400, 300))
    ui.open()
    ui.page.fill("#catname", "lesion")
    ui.page.press("#catname", "Enter")
    ui.page.wait_for_function("document.querySelectorAll('.cat').length===2")
    ui.drag(50, 50, 150, 150)                                 # drawn as "lesion", the newest class
    ui.page.keyboard.press("1")                               # then made an "object"
    ui.page.locator(".cat", has_text="lesion").locator("button[title^=Delete]").click()
    ui.page.wait_for_function("document.querySelectorAll('.cat').length===1")
    ui.page.keyboard.press("Control+z")
    assert ui.page.locator("#undo").is_disabled()
    assert ui.page.evaluate("annotator.state.boxes.map(b => b.category_id)") == [1]


def test_a_small_box_can_be_moved(ui, make_image):
    """In a box under two handles wide, every point used to be a corner, so dragging it resized it."""
    make_image("a.png", (400, 300))                           # shown at 1:1
    ui.open()
    ui.drag(100, 100, 114, 114)
    ui.click(107, 107)                                        # pick it, ready to move
    ui.drag(107, 107, 167, 147)
    assert ui.box() == pytest.approx([160, 140, 14, 14], abs=0.6)


def test_save_shows_what_was_saved(ui, make_image, dataset):
    """Boxes too small to keep are dropped on save, so they must not stay on screen as if saved."""
    make_image("a.png", (400, 300))
    seed(("a.png", 400, 300, [[10, 10, 2, 2], [50, 50, 40, 40]]))
    ui.open()
    assert ui.box_count == "2 boxes"
    ui.page.click("#save")
    # saving ends in the same step that redraws: the toast shows a little earlier
    ui.page.wait_for_function("!annotator.state.saving && document.getElementById('toast').textContent.startsWith('Saved 1')")
    assert ui.box_count == "1 box" and ui.box() == [50, 50, 40, 40]
    assert stored(dataset) == {"a.png": [[50, 50, 40, 40]]}


def test_closing_the_tab_with_unsaved_boxes_asks_first(ui, make_image):
    make_image("a.png", (400, 300))
    ui.open()
    ui.drag(50, 50, 150, 150)
    asked = []
    ui.page.on("dialog", lambda d: (asked.append(d.type), d.dismiss()))
    ui.page.close(run_before_unload=True)
    ui.until(lambda: asked)
    assert asked == ["beforeunload"]


def test_zoom_with_the_keyboard(ui, make_image):
    make_image("a.png", (400, 300))
    ui.open()
    for key, shown in [("+", "125%"), ("+", "156%"), ("-", "125%"), ("0", "100%")]:
        ui.page.keyboard.press(key)
        assert ui.text("#zoom") == shown, key


def test_export_warns_about_unsaved_boxes(ui, make_image):
    make_image("a.png", (400, 300))
    ui.open()
    ui.drag(50, 50, 150, 150)
    asked, sent = [], []
    ui.page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    ui.page.on("request", lambda r: "/api/export/" in r.url and sent.append(r))
    ui.page.click("#exportBtn")
    ui.page.click("#doExport")
    ui.until(lambda: asked)
    ui.page.evaluate("0")                                     # runs after the handler went on past the "no"
    assert "unsaved boxes" in asked[0] and not sent


def test_a_labels_file_that_was_set_aside_is_reported(ui, make_image, dataset):
    """It used to show only in the server's log: the user just saw their labels gone."""
    make_image("a.png", (400, 300))
    coco.COCO_PATH.write_text("{ not json", encoding="utf-8")   # e.g. edited by hand
    asked = []
    ui.page.on("dialog", lambda d: (asked.append(d.message), d.accept()))
    ui.open()
    ui.until(lambda: asked)
    assert "set aside" in asked[0] and "coco_annotations.corrupt-" in asked[0]
