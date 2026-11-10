"""Render operator-approved setup instructions for another machine's AI."""

from pathlib import Path
from urllib.parse import urlsplit

from agent_harness.pull_worker import validate_server

PLACEHOLDER = "{{TEAM_SERVICE_URL}}"

# One runbook per target platform; each is written to be read by an agent that
# has never seen this repository.
RUNBOOKS = {
    "linux": "linux-ai-setup.md",
    "windows": "windows-ai-setup.md",
    "macos": "macos-ai-setup.md",
}


def render_setup_prompt(template, server):
    """Render one runbook for an account-accessible HTTPS service."""
    validate_server(server)
    parsed = urlsplit(server)
    if parsed.scheme != "https":
        raise ValueError("另一台机器需要可远程访问的 HTTPS 团队服务地址")
    source = Path(template).read_text(encoding="utf-8")
    if source.count(PLACEHOLDER) != 1:
        raise RuntimeError("AI 配置提示词模板无效")
    return source.replace(PLACEHOLDER, server.rstrip("/"))
