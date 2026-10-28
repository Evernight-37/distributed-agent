"""Native production shell: a trusted local view and a narrow Python bridge."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading
import time
from pathlib import Path
from agent_harness.desktop_runtime import web_assets
from agent_harness.app_bridge import AppBridge


def request_close_dialog(window, bridge):
    """Ask the loaded page how to close, or safely close only the UI.

    Native close events can arrive before pywebview has loaded the page.  In
    that case there is no UI in which to offer the full-exit choice, so the
    safe fallback is the same as "continue in background": permit destruction
    of the native window without touching any service.
    """
    try:
        handled = window.evaluate_js(
            "typeof window.requestAppClose === 'function' ? (window.requestAppClose(), true) : false"
        )
    except Exception:
        handled = False
    if handled:
        return True
    bridge.allow_window_close = True
    try:
        window.destroy()
    except Exception:
        # The native close is already in progress on some pywebview backends.
        # Setting allow_window_close still lets the next close event complete.
        pass
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data")
    parser.add_argument("--smoke-report")
    parser.add_argument("--smoke-register", action="store_true")
    args = parser.parse_args()
    if args.smoke_register and not (args.data and args.smoke_report):
        parser.error("--smoke-register requires isolated --data and --smoke-report paths")
    import webview

    bridge = AppBridge(args.data)
    diagnostic_path = bridge.root / "app-errors.log"
    diagnostic_handler = RotatingFileHandler(
        diagnostic_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    if sys.platform != "win32":
        diagnostic_path.chmod(0o600)
    diagnostic_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    diagnostic_logger = logging.getLogger("agent_harness")
    diagnostic_logger.addHandler(diagnostic_handler)
    diagnostic_logger.setLevel(logging.ERROR)

    class API:
        def invoke(self, action, data=None):
            logging.getLogger(__name__).info("Bridge request: %s", action)
            result = bridge.invoke(action, data)
            logging.getLogger(__name__).info("Bridge response: %s ok=%s", action, result.get("ok"))
            return result

    window = webview.create_window(
        "Agent Harness · 协作工作台",
        url=str(web_assets() / "app.html"),
        js_api=API(),
        width=1220,
        height=820,
        min_size=(800, 600),
    )
    bridge.window = window

    def closing():
        if bridge.allow_window_close or args.smoke_report:
            return True
        # WinForms raises closing on its UI thread. Queue JavaScript after the
        # cancellation so a synchronous WebView call cannot deadlock the form.
        threading.Thread(target=request_close_dialog, args=(window, bridge), daemon=True).start()
        return False

    window.events.closing += closing
    window.events.closed += bridge.close

    def loaded():
        if args.smoke_report:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = window.evaluate_js(
                    "({title:document.title,login:!!document.getElementById('login'),bridge:typeof window.pywebview.api.invoke,heading:document.querySelector('#login h1')?.textContent,inputs:document.querySelectorAll('#login input').length})"
                )
                if result.get("heading"):
                    break
                time.sleep(0.05)
            import ssl

            try:
                context = ssl.create_default_context()
                result["tls_context"] = context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            except Exception as exc:
                result["tls_error"] = str(exc)
            if args.smoke_register:
                window.evaluate_js(
                    "window.__registrationProbe={pending:true};"
                    "window.pywebview.api.invoke('register',{username:'macOS packaged test',password:'',server:'http://127.0.0.1:8765'})"
                    ".then(v=>window.__registrationProbe=v).catch(e=>window.__registrationProbe={error:e.message});true"
                )
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    probe = window.evaluate_js("window.__registrationProbe")
                    if not probe.get("pending"):
                        break
                    time.sleep(0.1)
                result["registration"] = probe
                if probe.get("ok"):
                    window.evaluate_js("enter();true")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        heading = window.evaluate_js("document.querySelector('#content h1')?.textContent")
                        if heading == "控制台":
                            break
                        time.sleep(0.1)
                    result["dashboard_heading"] = heading
                from agent_harness.app_lifecycle import Lifecycle

                Lifecycle(bridge.root, host_root=bridge._host_state_root()).stop()
            Path(args.smoke_report).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            window.destroy()

    window.events.loaded += loaded
    webview.start(gui="cocoa" if sys.platform == "darwin" else "edgechromium", debug=False)


if __name__ == "__main__":
    main()
