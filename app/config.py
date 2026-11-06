"""
Settings shared by the annotator modules.

The dataset folder defaults to ./dataset at the repo root: the app's own
workspace, which the UI imports images into and can clear. Point the
ANNOTATOR_DATASET_DIR env var at your own folder to annotate images in place;
the UI then never deletes anything there.
"""

import logging
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATASET_DIR = Path(os.environ.get("ANNOTATOR_DATASET_DIR", ROOT_DIR / "dataset")).resolve()
MANAGED_WORKSPACE = "ANNOTATOR_DATASET_DIR" not in os.environ   # False = the user's own folder: never clear it

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("annotator")
