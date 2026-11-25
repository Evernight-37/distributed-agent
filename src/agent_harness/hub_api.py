"""Signed Hub account request boundary."""

from agent_harness.api_contracts import HUB_ACCOUNT


def account_response(request, response):
    if not isinstance(response, dict):
        raise RuntimeError("协作服务返回了无效数据")
    if "error" in response:
        # Older Hubs return task rows directly, including their execution diagnostic.
        body = request.get("envelope", {}).get("body", {})
        task_id = body.get("payload", {}).get("task")
        if (
            request.get("action") == "rpc"
            and body.get("operation") == "task-status"
            and task_id
            and response.get("id") == task_id
            and isinstance(response.get("project_id"), str)
            and isinstance(response.get("state"), str)
        ):
            return response
        raise RuntimeError(response["error"])
    return response.get("result", response) if response.get("ok") is True else response


class HubAccountAPI:
    def __init__(self, handler):
        self.handler = handler

    def dispatch(self, request):
        return self.handler(HUB_ACCOUNT.validate_request(request))

    @staticmethod
    def contract():
        return HUB_ACCOUNT.describe()
