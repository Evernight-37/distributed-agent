"""Protect uncommitted Git changes when a receiver switches project tasks."""

import hashlib
import os
from pathlib import Path
import subprocess

from agent_harness.common import read_json, write_json


DIRTY_WORKTREE = "Previous project task has uncommitted workspace changes"


def claim_worktree(root, path, work_id, git, changed_paths):
    """Called under the executor registry lock, before another task can start."""
    result = subprocess.run(
        [git, "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if result.returncode:
        return
    checkout = Path(result.stdout.strip()).resolve()
    key = hashlib.sha256(os.path.normcase(str(checkout)).encode()).hexdigest()
    marker = Path(root) / "worktree-owners" / (key + ".json")
    previous = read_json(marker, {}).get("work_id")
    if previous == work_id:
        return
    if previous and changed_paths(checkout):
        raise ValueError(DIRTY_WORKTREE)
    write_json(marker, {"work_id": work_id})
