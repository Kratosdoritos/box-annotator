# Changelog

## 2.2.1 (2026-11-04)

- The UI script is split into modules in `app/static/js/`: canvas, image list, classes, dialogs, undo history and shared state. Still no build step.
- A download cancelled half-way, or fetched in pieces by a download manager, no longer counts as exported (Clear then didn't warn) or deletes the file.
- Undo can no longer bring back a box of a deleted class, which a class added later would have taken over.
- A small box can be moved: dragging its middle used to resize it.
- After a save, the screen shows what was saved (boxes under 3 px are dropped).
- A save arriving during a Clear no longer leaves labels for a deleted image.
- A labels file set aside as unreadable is reported in the app, and Clear asks before deleting its images.
- Clear works only on the computer running the app; other sites can't show the app inside their pages.
- Imports read at most 50 MB, and a full disk is reported as such rather than as a bad file name.
- Prepared downloads are private to the user on Linux and macOS.
- A refused change (e.g. a class name already taken) no longer makes the next request re-read the labels file.
- Requires FastAPI 0.115.7+, Starlette 0.44+ and Pydantic 2.1.1+, the oldest versions now tested in CI.
- README: install steps for Windows and macOS / Linux, a warning about opening the app to the network, the full API, and the images' license in `docs/LICENSE.md`.

## 2.2.0 (2026-11-03)

- Zoom with the mouse wheel or `+` / `-`, pan with `Space`+drag or middle-drag, `0` for the whole image.
- Undo / redo every edit on an image (`Ctrl`+`Z` / `Ctrl`+`Y`).
- `←` / `→` switch images.
- The UI is split into `index.html`, `style.css` and `app.js`.
- Tests and lint (ruff) run on GitHub Actions: Ubuntu and Windows, Python 3.10 and 3.13.
- Requires Python 3.10+ and Pydantic 2.
- README: demo GIF, annotated screenshot, half the text.

## 2.1.0 (2026-11-02)

2.0.0 was never released on its own; its changes, the breaking one included, are listed here.

- Renamed from Cavity Annotator to Box Annotator, for any kind of image.
- Your own classes: add, rename, delete while unused, pick with `1`–`9`.
- Import a folder, images or a drop into a workspace; COCO labels come along.
- Export COCO JSON, COCO + images or YOLO, as a download or into a folder. YOLO also from the command line (`python -m app.yolo`).
- Clear the workspace when a batch is done; it warns if labels weren't exported.
- Fast with 20,000 images and 200,000 boxes.
- Refuses requests from other websites; sets aside unreadable label files; retries files another program has locked.
- **Breaking:** every box needs a `category_id`. Files from 1.0 still open.

## 1.0.0 (May 2026)

- Cavity Annotator: draw boxes on dental X-rays and save them as a COCO dataset, with atomic saves, corrupt-file backup and box clipping.
- Written for a university project with a medical team, and used there to label its X-rays.
- Moved into its own repository on 2026-11-01, with fixes and tests added in the move.
