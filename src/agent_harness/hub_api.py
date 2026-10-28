"""Signed Hub account request boundary."""

from agent_harness.api_contracts import HUB_ACCOUNT


class HubAccountAPI:
    def __init__(self, handler):
        self.handler = handler

    def dispatch(self, request):
        return self.handler(HUB_ACCOUNT.validate_request(request))

    @staticmethod
    def contract():
        return HUB_ACCOUNT.describe()
