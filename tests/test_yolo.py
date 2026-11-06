"""YOLO export tests: box conversion, negatives, the train/val split and output-folder safety."""

import pytest

from app import coco, yolo

from conftest import seed


def labels(out, split="train"):
    """Label file contents per file name for one split."""
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((out / "labels" / split).glob("*.txt"))}


def test_boxes_become_normalised_centre_lines(make_image, dataset):
    make_image("a.png", (100, 80))
    seed(("a.png", 100, 80, [[10, 20, 30, 40], [0, 0, 100, 80]]))
    out = dataset / "yolo"
    assert yolo.export_yolo(out, val_fraction=0) == {"train": 1, "val": 0, "boxes": 2}
    assert labels(out) == {"a.txt": "0 0.250000 0.500000 0.300000 0.500000\n"
                                    "0 0.500000 0.500000 1.000000 1.000000\n"}
    assert b"\r" not in (out / "labels/train/a.txt").read_bytes() + (out / "data.yaml").read_bytes()
    assert (out / "images/train/a.png").read_bytes() == (dataset / "images/a.png").read_bytes()
    config = (out / "data.yaml").read_text(encoding="utf-8")
    assert f'path: "{out.resolve().as_posix()}"\n' in config
    assert config.endswith('train: images/train\nval: images/train\nnames:\n  0: "object"\n')


def test_class_ids_map_to_consecutive_indices(make_image, dataset):
    make_image("a.png", (100, 80))
    seed(("a.png", 100, 80, [[0, 0, 10, 10, 3], [0, 0, 10, 10, 1]]), classes=("car", "bus", "van"))
    data = coco.load_coco()
    data["categories"] = [c for c in data["categories"] if c["id"] != 2]   # "bus" was deleted
    coco.save_coco(data)
    yolo.export_yolo(dataset / "yolo", val_fraction=0)
    assert [row.split()[0] for row in labels(dataset / "yolo")["a.txt"].splitlines()] == ["1", "0"]
    assert (dataset / "yolo/data.yaml").read_text(encoding="utf-8").endswith('names:\n  0: "car"\n  1: "van"\n')


def test_any_class_name_survives_data_yaml(make_image, dataset):
    yaml = pytest.importorskip("yaml")                      # what Ultralytics reads data.yaml with
    names = ["carie dentinaire é", "deep: caries", 'say "hi"', "#1 lesion", "tooth 😀", "null", "123", "日本語"]
    make_image("a.png", (100, 80))
    seed(("a.png", 100, 80, [[0, 0, 10, 10]]), classes=names)
    yolo.export_yolo(dataset / "yolo", val_fraction=0)
    got = yaml.safe_load((dataset / "yolo/data.yaml").read_text(encoding="utf-8"))["names"]
    assert [got[i] for i in range(len(names))] == names


def test_negatives_get_empty_labels_and_unsaved_images_are_skipped(make_image, dataset):
    for name in ["a.png", "b.png", "unsaved.png"]:
        make_image(name)
    seed(("a.png", 100, 80, [[1, 1, 5, 5]]), ("b.png", 100, 80, []), ("deleted.png", 100, 80, [[1, 1, 5, 5]]))
    out = dataset / "yolo"
    assert yolo.export_yolo(out, val_fraction=0) == {"train": 2, "val": 0, "boxes": 1}
    got = labels(out)
    assert sorted(got) == ["a.txt", "b.txt"] and got["b.txt"] == ""


def test_split_is_reproducible_and_complete(make_image, dataset):
    names = [f"{i:02}.png" for i in range(10)]
    for name in names:
        make_image(name)
    seed(*[(name, 100, 80, []) for name in names])

    def export(out):
        yolo.export_yolo(out, val_fraction=0.2, seed=7)
        for split in yolo.SPLITS:   # every image has its label beside it
            assert sorted(p.stem for p in (out / "images" / split).iterdir()) == sorted(p.stem for p in (out / "labels" / split).iterdir())
        return {split: sorted(p.name for p in (out / "images" / split).iterdir()) for split in yolo.SPLITS}

    first = export(dataset / "y1")
    assert len(first["val"]) == 2 and sorted(first["train"] + first["val"]) == names
    assert export(dataset / "y2") == first
    assert "val: images/val\n" in (dataset / "y1/data.yaml").read_text(encoding="utf-8")


def test_tiny_dataset_still_gets_a_val_image(make_image, dataset):
    make_image("a.png")
    make_image("b.png")
    seed(("a.png", 100, 80, []), ("b.png", 100, 80, []))
    assert yolo.export_yolo(dataset / "yolo") == {"train": 1, "val": 1, "boxes": 0}


@pytest.mark.parametrize("n, val", [(2, 0.75), (4, 0.9)])
def test_split_always_keeps_a_training_image(make_image, dataset, n, val):
    names = [f"{i}.png" for i in range(n)]
    for name in names:
        make_image(name)
    seed(*[(name, 100, 80, []) for name in names])
    counts = yolo.export_yolo(dataset / "yolo", val_fraction=val)
    assert counts["train"] >= 1 and counts["val"] >= 1


def test_overwrite_replaces_only_the_previous_export(make_image, dataset):
    make_image("a.png")
    make_image("b.png")
    seed(("a.png", 100, 80, []), ("b.png", 100, 80, []))
    out = dataset / "yolo"
    yolo.export_yolo(out, val_fraction=0)
    with pytest.raises(FileExistsError):
        yolo.export_yolo(out, val_fraction=0)

    seed(("a.png", 100, 80, []))                      # b.png is no longer saved
    (out / "notes.txt").write_text("mine", encoding="utf-8")
    (out / "labels/train.cache").write_bytes(b"stale")  # Ultralytics' label cache
    yolo.export_yolo(out, val_fraction=0, overwrite=True)
    assert labels(out) == {"a.txt": ""}               # no stale b.txt
    assert not (out / "labels/train.cache").exists()
    assert not (out / "images/train/b.png").exists()
    assert (out / "notes.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("sub", ["", "images", "images/yolo"])
def test_refuses_output_that_overlaps_the_source_images(make_image, dataset, sub):
    make_image("a.png")
    seed(("a.png", 100, 80, [[1, 1, 5, 5]]))
    with pytest.raises(ValueError, match="overlaps"):
        yolo.export_yolo(dataset / sub, overwrite=True)
    assert [p.name for p in (dataset / "images").iterdir()] == ["a.png"]


def test_label_name_clash_is_rejected(make_image, dataset):
    make_image("a.png")
    make_image("A.jpg")
    seed(("a.png", 100, 80, []), ("A.jpg", 100, 80, []))
    with pytest.raises(ValueError, match="share the label file"):
        yolo.export_yolo(dataset / "yolo")
    assert not (dataset / "yolo").exists()


def test_cli_writes_to_the_dataset_folder_and_reports_errors(make_image, dataset, capsys):
    with pytest.raises(SystemExit):
        yolo.main([])                                 # nothing saved yet
    assert "No saved images" in capsys.readouterr().err
    make_image("a.png")
    seed(("a.png", 100, 80, []))
    yolo.main(["--val", "0"])
    assert (dataset / "yolo/data.yaml").exists()
