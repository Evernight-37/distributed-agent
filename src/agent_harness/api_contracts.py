"""Versioned request contracts for process and network trust boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


API_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class BoundaryContract:
    """Describe and validate one action/object request boundary.

    Action-specific semantic validation remains with the owning API. This
    contract guarantees the common wire shape before any stateful handler runs.
    """

    name: str
    actions: frozenset[str]
    required_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # Action-local, transport-level validation.  This deliberately describes
    # shape and bounds only; authorization and stateful semantics stay in the
    # owning handler.
    field_schemas: Mapping[str, Mapping[str, Mapping[str, object]]] = field(default_factory=dict)
    version: int = API_CONTRACT_VERSION

    def __post_init__(self):
        unknown = set(self.required_fields) - self.actions
        if unknown:
            raise ValueError(f"{self.name} has schemas for unknown actions: {', '.join(sorted(unknown))}")
        normalized = {}
        for action, fields in self.required_fields.items():
            checked = frozenset(fields)
            if any(not isinstance(item, str) or not item for item in checked):
                raise ValueError(f"{self.name} action {action} has invalid required fields")
            normalized[action] = checked
        object.__setattr__(self, "required_fields", MappingProxyType(normalized))
        schema_unknown = set(self.field_schemas) - self.actions
        if schema_unknown:
            raise ValueError(f"{self.name} has schemas for unknown actions: {', '.join(sorted(schema_unknown))}")
        schemas = {}
        for action, fields in self.field_schemas.items():
            if not isinstance(fields, Mapping):
                raise ValueError(f"{self.name} action {action} field schema must be an object")
            checked = {}
            for name, rule in fields.items():
                if not isinstance(name, str) or not name or not isinstance(rule, Mapping):
                    raise ValueError(f"{self.name} action {action} has an invalid field schema")
                typ = rule.get("type")
                allowed = {"string", "integer", "boolean", "object", "array", "number"}
                valid_type = bool(typ) and set(typ) <= allowed if isinstance(typ, (list, tuple)) else typ in allowed
                if not valid_type:
                    raise ValueError(f"{self.name} action {action} field {name} has an invalid type")
                checked[name] = dict(rule)
            schemas[action] = MappingProxyType(checked)
        object.__setattr__(self, "field_schemas", MappingProxyType(schemas))

    @staticmethod
    def _matches(value, typ):
        if typ == "string":
            return isinstance(value, str)
        if typ == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if typ == "boolean":
            return isinstance(value, bool)
        if typ == "object":
            return isinstance(value, dict)
        if typ == "array":
            return isinstance(value, list)
        if typ == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return False

    def _validate_fields(self, action, payload):
        for name, rule in self.field_schemas.get(action, {}).items():
            if name not in payload:
                continue
            value = payload[name]
            types = rule["type"] if isinstance(rule["type"], (list, tuple)) else (rule["type"],)
            if not any(self._matches(value, typ) for typ in types):
                expected = ", ".join(types)
                raise ValueError(f"{self.name} action {action} field {name} must be {expected}")
            if isinstance(value, str):
                if "min_length" in rule and len(value) < rule["min_length"]:
                    raise ValueError(f"{self.name} action {action} field {name} is too short")
                if "max_length" in rule and len(value) > rule["max_length"]:
                    raise ValueError(f"{self.name} action {action} field {name} is too long")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if "minimum" in rule and value < rule["minimum"]:
                    raise ValueError(f"{self.name} action {action} field {name} is below minimum")
                if "maximum" in rule and value > rule["maximum"]:
                    raise ValueError(f"{self.name} action {action} field {name} is above maximum")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"{self.name} action {action} field {name} has an invalid value")
            if isinstance(value, list):
                if "max_items" in rule and len(value) > rule["max_items"]:
                    raise ValueError(f"{self.name} action {action} field {name} has too many items")
                item_type = rule.get("item_type")
                if item_type and any(not self._matches(item, item_type) for item in value):
                    raise ValueError(f"{self.name} action {action} field {name} has invalid items")

    def validate(self, action, payload=None):
        if not isinstance(action, str) or not action or len(action) > 100:
            raise ValueError(f"{self.name} action must be a non-empty string")
        if action not in self.actions:
            raise ValueError(f"Unknown {self.name} action: {action}")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"{self.name} payload must be an object")
        if any(not isinstance(key, str) for key in payload):
            raise ValueError(f"{self.name} payload keys must be strings")
        missing = self.required_fields.get(action, frozenset()) - payload.keys()
        if missing:
            fields = ", ".join(sorted(missing))
            raise ValueError(f"{self.name} action {action} requires fields: {fields}")
        self._validate_fields(action, payload)
        return action, payload

    def validate_request(self, request):
        if not isinstance(request, dict):
            raise ValueError(f"{self.name} request must be an object")
        if any(not isinstance(key, str) for key in request):
            raise ValueError(f"{self.name} request keys must be strings")
        action = request.get("action")
        self.validate(action, request)
        return request

    def describe(self):
        return {
            "name": self.name,
            "version": self.version,
            "request": {"type": "object", "action": "string", "additional_fields": True},
            "semantic_validation": "owning action handler",
            "actions": sorted(self.actions),
            "required_fields": {
                action: sorted(self.required_fields.get(action, frozenset())) for action in sorted(self.actions)
            },
            "field_schemas": {
                action: {name: dict(rule) for name, rule in self.field_schemas.get(action, {}).items()}
                for action in sorted(self.actions)
            },
        }


_TEXT = lambda maximum: {"type": "string", "max_length": maximum}
_ID = {"type": "string", "min_length": 1, "max_length": 120}
_EFFORT = {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]}
_TIMEOUT = {"type": "integer", "minimum": 30, "maximum": 86400}
_STAGES = {
    "type": "array",
    "max_items": 3,
    "item_type": "string",
}


DESKTOP_FIELDS = {
    "attachment-upload": {"project": _ID, "name": _TEXT(120), "base64": _TEXT(16_000_000)},
    "attachment-get": {"project": _ID, "id": _ID},
    "attachment-save": {"project": _ID, "id": _ID},
    "file-transfer-send-select": {
        "server": _TEXT(2048),
        "recipient_id": _ID,
        "recipient_node": _ID,
    },
    "file-transfer-accept": {"server": _TEXT(2048), "id": _ID, "recipient_node": _ID},
    "file-transfer-receive-folder": {
        "server": _TEXT(2048),
        "node": _ID,
        "path": _TEXT(4096),
    },
    "file-transfer-reject": {"server": _TEXT(2048), "id": _ID},
    "file-transfer-cancel": {"server": _TEXT(2048), "id": _ID},
    "file-transfer-job-status": {"id": _ID},
    "message": {"project": _ID, "body": _TEXT(20_000)},
    "memory-note-add": {"project": _ID, "body": _TEXT(20_000)},
    "employee-configure": {
        "id": _ID,
        "name": _TEXT(120),
        "model": _TEXT(100),
        "effort": _EFFORT,
        "enabled": {"type": "boolean"},
        "stages": _STAGES,
        "timeout": _TIMEOUT,
        "strict_worktree": {"type": "boolean"},
    },
    "workspace-configure": {"project": _ID, "node": _ID, "server": _TEXT(2048)},
    "warm-idle-save": {"seconds": {"type": "integer", "minimum": 0, "maximum": 86400}},
    "team-service-restore-apply": {"token": _TEXT(4096)},
    "recovery-export": {"recovery_password": {"type": "string", "min_length": 1, "max_length": 1024}},
    "recovery-import": {
        "server": _TEXT(2048),
        "recovery_password": {"type": "string", "min_length": 1, "max_length": 1024},
        "password": _TEXT(1024),
    },
    "linux-setup-prompt": {"server": _TEXT(2048)},
}


NODE_FIELDS = {
    "configure": {
        "project": _ID,
        "model": _TEXT(100),
        "effort": _EFFORT,
        "timeout": _TIMEOUT,
        "strict_worktree": {"type": "boolean"},
    },
    "register": {"project": {"type": "object"}, "memory": {"type": "object"}},
    "start": {"project": _ID, "prompt": _TEXT(1_000_000), "job": _ID},
    "memory": {"project": _ID, "expected_revision": {"type": "integer", "minimum": 0}, "memory": {"type": "object"}},
}


HUB_ACCOUNT_FIELDS = {
    "challenge": {"public_key": _TEXT(256)},
    "register": {"envelope": {"type": "object"}},
    "rpc": {"envelope": {"type": "object"}},
    "accept-project-invite": {"envelope": {"type": "object"}},
}


DESKTOP_CONTROL = BoundaryContract(
    "desktop-control",
    frozenset(
        {
            "app-background",
            "app-exit",
            "app-services",
            "approve-execution",
            "attachment-get",
            "attachment-save",
            "attachment-upload",
            "file-transfer-dashboard",
            "file-transfer-send-select",
            "file-transfer-accept",
            "file-transfer-reject",
            "file-transfer-cancel",
            "file-transfer-receive-folder",
            "file-transfer-job-status",
            "codex-cli-choose",
            "codex-cli-redetect",
            "codex-cli-save",
            "codex-cli-status",
            "codex-mcp-disable",
            "codex-mcp-enable",
            "codex-mcp-status",
            "conversation-read",
            "dashboard",
            "device-approve",
            "device-claim",
            "device-inspect",
            "device-request",
            "device-revoke",
            "diagnostics-get",
            "employee-configure",
            "employee-create",
            "employee-delete",
            "employee-delete-preview",
            "employee-dispatch",
            "employee-withdraw",
            "host-local-enable",
            "health-status",
            "invite",
            "join-project",
            "local-status",
            "login",
            "linux-setup-prompt",
            "logout",
            "memory-note-add",
            "memory-note-delete",
            "message",
            "new-project",
            "project",
            "proxy-save",
            "recovery-export",
            "recovery-import",
            "receiver-attach",
            "receiver-start",
            "receiver-stop",
            "register",
            "set-admin",
            "settings-get",
            "settings-save",
            "team-service-authorize",
            "team-service-backup",
            "team-service-copy",
            "team-service-create",
            "team-service-restore-apply",
            "team-service-restore-select",
            "team-service-status",
            "team-service-stop",
            "team-service-test",
            "warm-idle-save",
            "workspace-configure",
        }
    ),
    required_fields={
        "approve-execution": frozenset({"job_id"}),
        "attachment-get": frozenset({"project", "id"}),
        "attachment-save": frozenset({"project", "id"}),
        "attachment-upload": frozenset({"project", "name", "base64"}),
        "file-transfer-send-select": frozenset({"server", "recipient_id"}),
        "file-transfer-accept": frozenset({"server", "id"}),
        "file-transfer-reject": frozenset({"server", "id"}),
        "file-transfer-cancel": frozenset({"server", "id"}),
        "file-transfer-job-status": frozenset({"id"}),
        "conversation-read": frozenset({"project", "conversation_id"}),
        "device-approve": frozenset({"request_id"}),
        "device-inspect": frozenset({"code"}),
        "employee-configure": frozenset({"id", "name", "model", "effort", "enabled", "stages", "timeout"}),
        "employee-create": frozenset({"node", "name"}),
        "employee-delete": frozenset({"id"}),
        "employee-delete-preview": frozenset({"id"}),
        "employee-dispatch": frozenset({"id", "project"}),
        "employee-withdraw": frozenset({"id", "project"}),
        "invite": frozenset({"project"}),
        "join-project": frozenset({"invite"}),
        "login": frozenset({"profile"}),
        "linux-setup-prompt": frozenset({"server"}),
        "memory-note-add": frozenset({"project", "body"}),
        "memory-note-delete": frozenset({"project"}),
        "message": frozenset({"project", "body"}),
        "new-project": frozenset({"name"}),
        "project": frozenset({"id"}),
        "receiver-attach": frozenset({"server"}),
        "recovery-export": frozenset({"recovery_password"}),
        "recovery-import": frozenset({"server", "recovery_password", "password"}),
        "register": frozenset({"username"}),
        "set-admin": frozenset({"project", "agent"}),
        "team-service-restore-apply": frozenset({"token"}),
        "warm-idle-save": frozenset({"seconds"}),
        "workspace-configure": frozenset({"project", "node"}),
    },
    field_schemas=DESKTOP_FIELDS,
)

NODE_EXECUTION = BoundaryContract(
    "node-execution",
    frozenset(
        {
            "cancel",
            "configure",
            "execution_cancel",
            "execution_start",
            "execution_status",
            "execution_templates",
            "fetch",
            "health",
            "memory",
            "mkdir_demo",
            "probe",
            "project",
            "register",
            "start",
            "status",
        }
    ),
    required_fields={
        "cancel": frozenset({"job"}),
        "configure": frozenset({"project", "model", "effort", "timeout"}),
        "execution_cancel": frozenset({"execution"}),
        "execution_start": frozenset({"project", "execution", "template"}),
        "execution_status": frozenset({"execution"}),
        "execution_templates": frozenset({"project", "templates"}),
        "fetch": frozenset({"job"}),
        "memory": frozenset({"project", "expected_revision", "memory"}),
        "project": frozenset({"project"}),
        "register": frozenset({"project", "memory"}),
        "start": frozenset({"project", "prompt", "job"}),
        "status": frozenset({"job"}),
    },
    field_schemas=NODE_FIELDS,
)

HUB_ACCOUNT = BoundaryContract(
    "hub-account",
    frozenset(
        {
            "accept-project-invite",
            "activate-device",
            "challenge",
            "claim-device",
            "register",
            "request-device",
            "rpc",
        }
    ),
    required_fields={
        "accept-project-invite": frozenset({"envelope"}),
        "activate-device": frozenset({"envelope"}),
        "challenge": frozenset({"public_key"}),
        "claim-device": frozenset({"request_id", "code"}),
        "register": frozenset({"envelope"}),
        "request-device": frozenset({"public_key"}),
        "rpc": frozenset({"envelope"}),
    },
    field_schemas=HUB_ACCOUNT_FIELDS,
)
