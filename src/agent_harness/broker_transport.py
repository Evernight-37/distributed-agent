"""Optional, explicitly configured loopback proxy for a userspace tailnet."""

import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def broker_opener(proxy=None):
    proxies = {}
    if proxy:
        parsed = urlsplit(proxy)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("协作代理必须是本机 HTTP 代理地址，例如 http://127.0.0.1:18766")
        proxies = {"http": proxy, "https": proxy}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
