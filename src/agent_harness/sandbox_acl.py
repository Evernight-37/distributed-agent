"""Sandbox write access for a granted agent.

On Windows the DeepSeek Harness shell sandbox runs a restricted token whose group
membership decides what an agent may write. A project directory gets its own
provisioning; a granted turn needs the same treatment for the places the grant
promises, so the sandbox group is given Modify there and the entry is removed when
the grant goes away.
"""

import subprocess
import sys
from pathlib import Path

# The local alias whose members are the sandbox's restricted identities. It exists
# only on machines where the sandbox was installed; without it nothing is granted.
SANDBOX_GROUP = "CodexSandboxUsers"


def granted_paths(home=None):
    """The places a full-access grant covers: the user's own profile."""
    return [Path(home) if home is not None else Path.home()]


def command(identity, group, path, allow):
    """The icacls argv for one grant or revoke; no shell, no wildcards."""
    target = str(Path(path))
    # Inherited entries are evaluated at access time, so no /T walk is needed.
    if allow:
        return [identity, target, "/grant", group + ":(OI)(CI)M", "/C", "/Q"]
    return [identity, target, "/remove:g", group, "/C", "/Q"]


def has_group(identity, group):
    """Whether the sandbox group exists on this machine."""
    try:
        result = subprocess.run(
            [identity, "localgroup", group],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def apply(path, allow, run=None, identity=None, group=SANDBOX_GROUP):
    """Grant or revoke sandbox write access; returns whether anything ran."""
    if sys.platform != "win32":
        return False
    run = run or subprocess.run
    identity = identity or "icacls"
    if allow and not has_group("net", group):
        return False
    argv = command(identity, group, path, allow)
    try:
        run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
