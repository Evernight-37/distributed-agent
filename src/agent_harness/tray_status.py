"""Account-scoped unread indicators and local service status for the tray."""

import threading


class TrayStatus:
    def __init__(self, tray, account, services, background, language=lambda: "zh-CN"):
        self.tray = tray
        self.account = account
        self.services = services
        self.background = background
        self.language = language
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.account_id = None
        self.inboxes = {}

    def _switch_account(self, account_id):
        if self.account_id != account_id:
            self.account_id = account_id
            self.inboxes.clear()

    def attention(self, account_id, server, cursor, summary):
        if self.stopping.is_set() or not isinstance(summary, dict):
            return
        count = summary.get("unread_count")
        latest = summary.get("latest_unread_id")
        if type(count) is not int or count < 0 or type(latest) is not int or latest < 0:
            return
        key = summary.get("service_id") or server
        if not isinstance(key, str):
            return
        with self.lock:
            if account_id != self.account():
                return
            self._switch_account(account_id)
            previous = self.inboxes.get(key)
            if previous and cursor <= previous[0]:
                return
            peak = max(previous[2], latest) if previous else latest
            self.inboxes[key] = (cursor, count, peak)
            new_message = previous and count > 0 and latest > previous[2]
        if new_message and self.background.is_set() and not self.stopping.is_set() and account_id == self.account():
            self.tray.notify()

    def title(self):
        account_id = self.account()
        with self.lock:
            self._switch_account(account_id)
            unread = sum(entry[1] for entry in self.inboxes.values())
        host, receivers = self.services()
        chinese = self.language() == "zh-CN"

        def text(zh, en):
            return zh if chinese else en

        parts = [text("本机 Hub 运行中", "Local hub running") if host else text("本机 Hub 未运行", "Local hub stopped")]
        if receivers:
            running = sum(bool(receiver["running"]) for receiver in receivers)
            count = f"{running}/{len(receivers)}"
            parts.append(text(f"接收端 {count} 运行", f"Receivers {count} running"))
        if account_id is None:
            parts.append(text("账户未解锁", "Account locked"))
        elif unread:
            count = unread if unread < 100 else "99+"
            parts.append(text(f"{count} 条未读", f"{count} unread"))
        return " · ".join(parts)

    def start(self):
        threading.Thread(target=self._run, name="tray-status", daemon=True).start()

    def _run(self):
        while not self.stopping.is_set():
            try:
                status = self.title()
            except (OSError, ValueError, RuntimeError):
                status = "暂时无法读取后台状态" if self.language() == "zh-CN" else "Background status unavailable"
            if self.stopping.is_set():
                return
            self.tray.set_status(status)
            self.stopping.wait(5)

    def stop(self):
        self.stopping.set()
