"""Runtime paths shared by the local panel and packaged application."""

import os
import sys
from pathlib import Path


def application_home():
    """Keep user data outside the app, including read-only mounted disk images."""
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "AgentHarness"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AgentHarness"
    return Path.home() / "AgentHarness"


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"
DEFAULT_STATE_ROOT = application_home()
DEFAULT_STATE = Path(os.environ.get("AGENT_HARNESS_STATE", str(DEFAULT_STATE_ROOT / "data")))
