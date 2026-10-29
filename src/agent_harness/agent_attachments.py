"""Deliver model-selected project files without exposing device credentials to Codex."""

import base64
import json
import os
import stat
import urllib.error
from contextlib import ExitStack
from pathlib import Path

from agent_harness.attachments import AGENT_ATTACHMENT_FEATURE, MAX_ATTACHMENTS, MAX_FILE_BYTES
from agent_harness.common import read_json, write_json


def read_project_file(root, requested):
    root = Path(root).resolve(strict=True)
    if not isinstance(requested, str) or not requested or len(requested) > 4096:
        raise ValueError("附件路径无效")
    path = Path(requested)
    try:
        relative = path.relative_to(root) if path.is_absolute() else path
    except ValueError:
        raise ValueError("附件必须位于当前项目目录") from None
    if not relative.parts or any(part.startswith(".") for part in relative.parts):
        raise ValueError("不能发送隐藏文件、内部配置或越界路径")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as stack:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, directory)
            for part in relative.parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(relative.name, flags, dir_fd=directory)
        else:
            candidate = root
            for part in relative.parts:
                candidate = candidate / part
                if candidate.is_symlink():
                    raise ValueError("不能发送链接文件")
            if not candidate.resolve(strict=True).is_relative_to(root):
                raise ValueError("附件必须位于当前项目目录")
            descriptor = os.open(candidate, flags)
        stream = stack.enter_context(os.fdopen(descriptor, "rb"))
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("只能发送普通文件")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("每个附件最大 5 MB")
        content = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(content) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("附件在读取时发生变化，请重新发送")
    return relative.name, content


def prepare_reply(connector, receipt, features):
    """Upload once per slot, retry network failures, and strip local paths before reporting."""
    if receipt.get("attachment_delivery_complete"):
        return
    result, job = receipt.get("result", {}), receipt["job"]
    if job["kind"] != "task" or result.get("status") != "needs_review":
        return
    try:
        answer = json.loads(result.get("report", ""))
    except (ValueError, TypeError):
        return
    if not isinstance(answer, dict) or not isinstance(answer.get("reply", ""), str) or not answer.get("attachments"):
        return
    requested = answer.pop("attachments")
    errors = []
    path = connector.receipt_path(str(job.get("job_id", job.get("id"))))
    if not isinstance(requested, list) or len(requested) > MAX_ATTACHMENTS:
        errors.append("每条回复最多 5 个附件")
        requested = []
    if AGENT_ATTACHMENT_FEATURE not in features:
        errors.append("托管端需要升级以支持 Agent 附件")
        requested = []
    config = read_json(connector.root / "jobs" / receipt["local_id"] / "request.json", {})
    root = config.get("project", {}).get("remote_path")
    uploads = receipt.setdefault("attachment_uploads", {})
    ids = []
    for slot, item in enumerate(requested):
        if str(slot) in uploads:
            ids.append(uploads[str(slot)])
            continue
        try:
            if not root or not isinstance(item, dict) or set(item) != {"path"}:
                raise ValueError("附件描述无效")
            name, content = read_project_file(root, item["path"])
        except (OSError, ValueError):
            errors.append(f"附件 {slot + 1} 无法读取：仅支持项目内的普通文件，每个最大 5 MB")
            continue
        try:
            metadata = connector.transport(
                connector.config,
                "task-attachment",
                {
                    "job_id": job.get("job_id", job.get("id")),
                    "lease_id": job["lease_id"],
                    "slot": slot,
                    "name": name,
                    "base64": base64.b64encode(content).decode("ascii"),
                },
            )
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code in {408, 429}:
                raise
            errors.append(f"附件 {slot + 1} 被协作服务拒绝（HTTP {exc.code}）")
            continue
        except (ValueError, PermissionError):
            errors.append(f"附件 {slot + 1} 未通过服务验证")
            continue
        uploads[str(slot)] = metadata["id"]
        ids.append(metadata["id"])
        write_json(path, receipt)
    answer["attachment_ids"] = ids
    if ids and not answer.get("reply", "").strip():
        answer["reply"] = "已发送附件。"
    if errors:
        answer["reply"] = (answer.get("reply", "") + "\n\n附件发送提示：" + "；".join(errors)).strip()[-16000:]
    result["report"] = json.dumps(answer, ensure_ascii=False)
    receipt["attachment_delivery_complete"] = True
    write_json(path, receipt)
