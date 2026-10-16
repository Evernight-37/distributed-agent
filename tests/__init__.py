"""Test bootstrap for the repository's src layout."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / 'src' / 'agent_harness'
SCRIPTS_ROOT = PROJECT_ROOT / 'scripts'
for path in (SOURCE_ROOT, SCRIPTS_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
