"""Render operator-approved setup instructions for a remote Linux Codex."""

from pathlib import Path
from urllib.parse import urlsplit

from agent_harness.pull_worker import validate_server


PLACEHOLDER = "{{TEAM_SERVICE_URL}}"


def render_linux_setup_prompt(template, server):
    """Render the bundled prompt for one account-accessible HTTPS service."""

    validate_server(server)
    parsed = urlsplit(server)
    if parsed.scheme != "https":
        raise ValueError("Linux 服务器需要可远程访问的 HTTPS 团队服务地址")
    source = Path(template).read_text(encoding="utf-8")
    if source.count(PLACEHOLDER) != 1:
        raise RuntimeError("Linux AI 配置提示词模板无效")
    return source.replace(PLACEHOLDER, server.rstrip("/"))
