"""The executor and Hub share one allowlisted project task action contract."""

from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from agent_harness.attachments import MAX_ATTACHMENTS


FILE_DELIVERY_FEATURE = "project_file_delivery_v1"
FILE_DELIVERY_PENDING = "pending_attachment_delivery"

PROJECT_TOOLS = {
    "project_draft_create": (
        "create_draft",
        "Save a new draft for human confirmation. The Hub assigns its ID and version.",
    ),
    "project_draft_revise": (
        "revise_draft",
        "Save changes to a draft. Include only fields that need to change. The Hub checks and increments the version.",
    ),
    "project_task_follow_up": (
        "follow_up",
        "Add the human's requested instruction to the matching project task.",
    ),
    "project_task_deliver": (
        "deliver",
        "Register the actual result for the assigned project task, with any HTTP or HTTPS links.",
    ),
}


def action_policy(context):
    policy = context.get("project_action_policy") if isinstance(context, dict) else None
    return policy if isinstance(policy, dict) and policy.get("role") in {"admin", "executor", "chat"} else None


def _object(kind, **fields):
    properties = {"kind": {"type": "string", "enum": [kind]}, **fields}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def action_schema(policy):
    """Schema accepted by both the Codex output API and the DSH reply tool."""
    role = policy["role"]
    actions = [{"type": "null"}]
    text = {"type": "string"}
    if role == "admin":
        agents = policy.get("agents", [])
        if agents:
            fields = {
                key: text
                for key in ("title", "instructions", "deliverable", "acceptance", "validation", "allowed_files")
            }
            fields.update(
                agent={"type": "string", "enum": agents},
                stage={"type": "string", "enum": ["discuss", "implement"]},
                reference_message_ids={"type": "array", "items": {"type": "integer"}},
            )
            spec = {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
            if not policy.get("bound_draft"):
                actions.append(_object("create_draft", spec=spec))
            if drafts := policy.get("drafts"):
                actions.append(
                    _object(
                        "revise_draft",
                        draft_id={"type": "string", "enum": [row["id"] for row in drafts]},
                        base_revision={"type": "integer"},
                        spec=spec,
                    )
                )
        if (works := policy.get("works")) and not policy.get("bound_draft"):
            actions.append(
                _object(
                    "follow_up",
                    work_id={"type": "string", "enum": works},
                    instruction={"type": ["string", "null"]},
                )
            )
    elif role == "executor":
        actions.extend(
            (_object("deliver", summary=text, links={"type": "array", "items": text}), _object("wait", question=text))
        )
    return {"anyOf": actions} if len(actions) > 1 else actions[0]


def _matches(value, schema):
    types = schema.get("type")
    if isinstance(types, list):
        return any(_matches(value, {**schema, "type": item}) for item in types)
    if "enum" in schema and value not in schema["enum"]:
        return False
    if types == "null":
        return value is None
    if types == "string":
        return isinstance(value, str) and schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16000)
    if types == "integer":
        return type(value) is int and 0 < value < 2**63
    if types == "array":
        return (
            isinstance(value, list)
            and len(value) <= schema.get("maxItems", 30)
            and all(_matches(item, schema["items"]) for item in value)
            and (not schema.get("uniqueItems") or len(value) == len(set(value)))
        )
    properties = schema["properties"]
    return (
        isinstance(value, dict)
        and value.keys() == properties.keys()
        and all(_matches(value[key], rule) for key, rule in properties.items())
    )


def _validate_delivery(value):
    if not value["summary"].strip() or len(value["links"]) > 20:
        raise ValueError("summary 不能为空，links 最多 20 条")
    for link in value["links"]:
        try:
            url = urlsplit(link)
            if (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.username is None
                and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in link)
            ):
                continue
        except ValueError:
            pass
        raise ValueError("links 只能填写 http:// 或 https:// 链接。本地文件请填入 attachments。")
    for attachment in value.get("attachments", []):
        posix = PurePosixPath(attachment)
        windows = PureWindowsPath(attachment)
        if (
            not attachment.strip()
            or posix.is_absolute()
            or windows.anchor
            or any(char == ":" or ord(char) < 32 for char in attachment)
            or not windows.parts
            or any(part.startswith(".") for part in windows.parts)
        ):
            raise ValueError("attachments 只能填写项目内普通文件的相对路径。")


def validate_action(value, policy):
    schema = action_schema(policy)
    if not any(_matches(value, rule) for rule in schema.get("anyOf", [schema])):
        raise ValueError("任务操作不符合本轮允许的格式")
    if value is None:
        return None
    kind = value["kind"]
    if kind == "revise_draft" and not any(
        row["id"] == value["draft_id"] and row["revision"] == value["base_revision"] for row in policy["drafts"]
    ):
        raise ValueError("任务草稿版本与本轮授权不一致")
    if kind == "deliver":
        _validate_delivery(value)
    if kind == "wait" and not value["question"].strip():
        raise ValueError("等待说明不能为空")
    return value


def tool_specs(policy, *, file_delivery=False):
    """Describe the task tools enabled for the trusted turn scope."""
    if policy is None:
        return []
    schema = action_schema(policy)
    actions = {
        branch["properties"]["kind"]["enum"][0]: branch["properties"]
        for branch in schema.get("anyOf", [schema])
        if branch.get("type") == "object"
    }
    tools = []
    for name, (kind, description) in PROJECT_TOOLS.items():
        if kind not in actions:
            continue
        fields = {field: schema for field, schema in actions[kind].items() if field != "kind"}
        if kind == "revise_draft":
            if policy.get("bound_draft"):
                if len(policy["drafts"]) != 1:
                    continue
                fields.pop("draft_id")
            fields.pop("base_revision")
            fields["changes"] = {**fields.pop("spec"), "required": [], "minProperties": 1}
        required = list(fields)
        if kind == "deliver" and file_delivery:
            description = (
                "Submit text, web links and project files together. Use attachments for project-relative file paths. "
                "A pending attachment receipt is not a published delivery; finish the turn normally so it can proceed."
            )
            fields["attachments"] = {
                "type": "array",
                "description": "Project-relative paths of files to attach. Omit or leave empty for text/link delivery.",
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                "maxItems": MAX_ATTACHMENTS,
                "uniqueItems": True,
            }
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": fields,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return tools


def project_tool_action(name, arguments):
    """Translate a native tool request to a task action without granting authority."""
    if not isinstance(name, str) or name not in PROJECT_TOOLS:
        raise ValueError("未知的任务工具")
    if not isinstance(arguments, dict) or "kind" in arguments:
        raise ValueError("任务工具参数必须为对象且不能指定 kind")
    return {"kind": PROJECT_TOOLS[name][0], **arguments}


def _validate_tool_input(value, schema, path=()):
    field = ".".join(path) or "参数"
    if schema["type"] != "object":
        if not _matches(value, schema):
            raise ValueError(f"{field} 的值无效或不在本轮允许范围内")
        return
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须为对象")
    properties = schema["properties"]
    for key in value:
        if key not in properties:
            raise ValueError(f"{field} 不接受字段 {key}")
    for key in schema["required"]:
        if key not in value:
            raise ValueError(f"缺少字段 {'.'.join((*path, key))}")
    if len(value) < schema.get("minProperties", 0):
        raise ValueError(f"{field} 至少需要一个修改字段")
    for key, item in value.items():
        _validate_tool_input(item, properties[key], (*path, key))


def resolve_tool_action(action, policy, *, file_delivery=False):
    """Validate an action and resolve its draft target from the dispatched scope."""
    kind = action.get("kind") if isinstance(action, dict) else None
    allowed = {
        PROJECT_TOOLS[tool["name"]][0]: tool["inputSchema"] for tool in tool_specs(policy, file_delivery=file_delivery)
    }
    if not isinstance(kind, str) or kind not in allowed:
        raise ValueError("本轮不允许此任务操作")
    _validate_tool_input({key: value for key, value in action.items() if key != "kind"}, allowed[kind])
    if kind == "revise_draft":
        draft = (
            policy["drafts"][0]
            if policy.get("bound_draft")
            else next(row for row in policy["drafts"] if row["id"] == action["draft_id"])
        )
        return {**action, "draft_id": draft["id"], "base_revision": draft["revision"]}
    if kind == "deliver":
        _validate_delivery(action)
    return action
