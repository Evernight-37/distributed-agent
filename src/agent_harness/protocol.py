"""Explicit compatibility contract for Desktop/Node/MCP to Hub requests."""

HUB_PROTOCOL = 1
CLIENT_PROTOCOL = 1
MINIMUM_HUB_PROTOCOL = 1
MINIMUM_CLIENT_PROTOCOL = 1


def hub_advertisement():
    return {"hub": HUB_PROTOCOL, "minimum_client": MINIMUM_CLIENT_PROTOCOL}


def ensure_hub_compatible(challenge):
    """Validate new Hub advertisements while accepting 1.1 legacy Hubs."""

    value = challenge.get("protocol") if isinstance(challenge, dict) else None
    if value is None:
        return {"legacy": True}
    if not isinstance(value, dict):
        raise RuntimeError("协作服务返回了无效的协议版本")
    hub = value.get("hub")
    minimum_client = value.get("minimum_client")
    if (
        not isinstance(hub, int)
        or isinstance(hub, bool)
        or not isinstance(minimum_client, int)
        or isinstance(minimum_client, bool)
    ):
        raise RuntimeError("协作服务返回了无效的协议版本")
    if hub < MINIMUM_HUB_PROTOCOL:
        raise RuntimeError("协作服务版本过旧，请先升级托管端")
    if minimum_client > CLIENT_PROTOCOL:
        raise RuntimeError("当前 Agent Harness 版本过旧，请升级本机应用")
    return {"legacy": False, "hub": hub, "minimum_client": minimum_client}
