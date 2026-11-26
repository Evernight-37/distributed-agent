"""Native production shell: a trusted local view and a narrow Python bridge."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading
import time
from pathlib import Path
from agent_harness import desktop_locale
from agent_harness.app_lifecycle import Lifecycle
from agent_harness.desktop_runtime import web_assets
from agent_harness.app_bridge import AppBridge
from agent_harness.instance import Instance
from agent_harness import tray as tray_module
from agent_harness.tray_status import TrayStatus


def request_close_dialog(window, bridge):
    """Ask the loaded page how to close, or leave the services running.

    Before the page is ready, use the tray or Dock background handler if present.
    Otherwise close the native window and leave the services running.
    """
    try:
        handled = window.evaluate_js(
            "typeof window.requestAppClose === 'function' ? (window.requestAppClose(), true) : false"
        )
    except Exception:
        handled = False
    if handled:
        return True
    if callable(getattr(bridge, "on_background", None)):
        bridge.on_background()
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
    parser.add_argument("--tray", action="store_true", help="Start in the Windows notification area")
    parser.add_argument("--smoke-report")
    parser.add_argument("--smoke-register", action="store_true")
    args = parser.parse_args()
    if args.smoke_register and not (args.data and args.smoke_report):
        parser.error("--smoke-register requires isolated --data and --smoke-report paths")
    import webview

    bridge = AppBridge(args.data)
    holder = {}
    tray_available = tray_module.available()
    tray_requested = args.tray and tray_available
    background = threading.Event()
    minimized = threading.Event()
    ready = threading.Event()
    show_requested = threading.Event()

    def show_window():
        window = holder.get("window")
        if window is not None:
            if minimized.is_set():
                window.restore()
            window.show()
            background.clear()

    def raise_window():
        show_requested.set()
        if ready.is_set():
            show_requested.clear()
            threading.Thread(target=show_window, name="show-window", daemon=True).start()

    instance = Instance(bridge.root)
    if not instance.claim(on_show=raise_window, show_existing=not tray_requested):
        return 0
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

    # Only the process that owns the data directory can try the saved unlock.
    try:
        bridge.auto_login()
    except Exception:
        bridge.autologin_failed = True

    language = desktop_locale.resolved_language(desktop_locale.settings(bridge.root))
    tray = None

    class API:
        def invoke(self, action, data=None):
            nonlocal language
            logging.getLogger(__name__).info("Bridge request: %s", action)
            if background.is_set() and action == "project" and isinstance(data, dict):
                data = {key: value for key, value in data.items() if key != "active_conversation_id"}
            result = bridge.invoke(action, data)
            if action in {"interface-language", "set-interface-language"} and result.get("ok"):
                updated = desktop_locale.resolved_language(result["result"])
                if updated != language:
                    language = updated
                    try:
                        window.set_title(desktop_locale.window_title(language))
                    except Exception:
                        diagnostic_logger.exception("Could not update the native window language")
                    if tray is not None:
                        tray.refresh_menu()
            logging.getLogger(__name__).info("Bridge response: %s ok=%s", action, result.get("ok"))
            return result

    from agent_harness import brand

    brand.refresh_shell_icons(bridge.root)
    window = webview.create_window(
        desktop_locale.window_title(language),
        url=str(web_assets() / "app.html"),
        js_api=API(),
        width=1220,
        height=820,
        min_size=(800, 600),
        hidden=tray_requested,
    )
    bridge.window = window
    holder["window"] = window

    tray_status = None
    if tray_available:

        def quit_from_tray():
            threading.Thread(target=lambda: bridge.invoke("app-exit", {}), daemon=True).start()

        def hide_window():
            background.set()
            window.hide()

        def restored():
            minimized.clear()
            background.clear()

        def minimize_window():
            minimized.set()
            background.set()

        def local_services():
            status = Lifecycle(bridge.root, host_root=bridge._host_state_root()).status()
            return status["host"]["running"], status["receivers"]

        tray = tray_module.Tray("Foolship", raise_window, quit_from_tray, language=lambda: language)
        if tray.start():
            if tray_requested:
                background.set()
            bridge.on_background = hide_window
            window.events.minimized += minimize_window
            window.events.restored += restored
            tray_status = TrayStatus(
                tray,
                lambda: (bridge.identity or {}).get("account_id"),
                local_services,
                background,
                language=lambda: language,
            )
            bridge.tray_attention = tray_status.attention
            tray_status.start()
        else:
            tray.stop()
            tray = None
            window.hidden = False
    elif sys.platform == "darwin":
        # No tray on macOS, but a minimised window is restored from the Dock.
        bridge.on_background = window.minimize

    def closing():
        if bridge.allow_window_close or args.smoke_report:
            return True
        # WinForms raises closing on its UI thread. Queue JavaScript after the
        # cancellation so a synchronous WebView call cannot deadlock the form.
        threading.Thread(target=request_close_dialog, args=(window, bridge), daemon=True).start()
        return False

    window.events.closing += closing

    def closed():
        bridge.tray_attention = None
        if tray_status is not None:
            tray_status.stop()
        if tray is not None:
            tray.stop()
        instance.release()
        bridge.close()

    window.events.closed += closed

    def loaded():
        ready.set()
        bridge._sync_event_pump()
        if show_requested.is_set():
            show_requested.clear()
            show_window()
        if args.smoke_report:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = window.evaluate_js(
                    "({title:document.title,login:!!document.getElementById('login'),bridge:typeof window.pywebview.api.invoke,heading:document.querySelector('#login h1:not(.startup-title)')?.textContent,language:document.documentElement.lang,inputs:document.querySelectorAll('#login input').length})"
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
            result["tray_ready"] = bool(tray and tray.ready)
            result["starts_in_tray"] = background.is_set()
            if sys.platform == "win32":
                result["window_visible"] = bool(window.native.Visible)
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
                        if heading in {"控制台", "Home"}:
                            break
                        time.sleep(0.1)
                    result["dashboard_heading"] = heading
                Lifecycle(bridge.root, host_root=bridge._host_state_root()).stop()
            Path(args.smoke_report).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            window.destroy()

    window.events.loaded += loaded
    webview.start(
        gui="cocoa" if sys.platform == "darwin" else "edgechromium",
        debug=False,
        icon=brand.window_icon(bridge.root),
    )


if __name__ == "__main__":
    main()
