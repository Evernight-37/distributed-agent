"""Runtime paths shared by the local panel and packaged application."""

import os
from pathlib import Path


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"
DEFAULT_STATE_ROOT = PROJECT_ROOT.parent if SOURCE_LAYOUT else SOURCE.parent
DEFAULT_STATE = Path(os.environ.get("AGENT_HARNESS_STATE", str(DEFAULT_STATE_ROOT / "agent-harness-data")))
