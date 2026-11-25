"""Foolship test suite.

The model table is seeded from the machine's Codex configuration, so the suite
supplies its own Codex home instead of depending on whoever runs it. Without
this, a developer with a configured Codex and a CI runner without one would see
different default tables, and fixtures that dispatch a model would pass
everywhere except where it is missing.
"""

import os
import re
import tempfile
from pathlib import Path

# Temporary repositories should not inherit the caller's per-process Git config.
for _key in list(os.environ):
    if re.fullmatch(r"GIT_CONFIG_(?:COUNT|(?:KEY|VALUE)_[0-9]+)", _key):
        del os.environ[_key]

if not os.environ.get("CODEX_HOME"):
    _home = Path(tempfile.mkdtemp(prefix="harness-test-codex-"))
    (_home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    os.environ["CODEX_HOME"] = str(_home)
