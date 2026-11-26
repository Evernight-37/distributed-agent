"use strict";
let eventRefreshPending = false;
let eventRefreshTimer = null;

function drainEventRefresh() {
  eventRefreshTimer = null;
  if (!eventRefreshPending) return;
  if (document.hidden) return;
  if (polling || !S.dash) {
    eventRefreshTimer = setTimeout(drainEventRefresh, 100);
    return;
  }
  eventRefreshPending = false;
  refresh().catch(() => {
    eventRefreshPending = true;
    if (!eventRefreshTimer)
      eventRefreshTimer = setTimeout(drainEventRefresh, 1000);
  });
}

window.__harnessEvent = () => {
  eventRefreshPending = true;
  if (!document.hidden && !eventRefreshTimer)
    eventRefreshTimer = setTimeout(drainEventRefresh, 60);
};

async function refresh(force = false) {
  if (quitDialog || polling || !S.dash) return;
  polling = true;
  const epoch = S.epoch,
    project = S.project,
    page = S.page;
  try {
    const dash = await api("dashboard");
    let view = null;
    if (page === "project") {
      const service = S.view?.server || S.view?.project?.server;
      if (
        service &&
        !dash.unavailable_services?.includes(service) &&
        dash.services?.some((item) => item.server === service) &&
        !dash.projects.some((item) => item.id === project)
      ) {
        if (epoch === S.epoch && project === S.project) {
          closeModal();
          S.dash = dash;
          renderDashboard();
        }
        return;
      }
      const selected = S.chatSelection?.get(project);
      view = await api("project", {
        id: project,
        visit: false,
        active_conversation_id:
          !document.hidden && selected && !selected.startsWith("new:")
            ? selected
            : undefined,
      });
    }
    if (epoch !== S.epoch || project !== S.project || page !== S.page) return;
    const changed =
      S.dirty ||
      JSON.stringify(dash) !== JSON.stringify(S.dash) ||
      (view && JSON.stringify(view) !== JSON.stringify(S.view));
    S.dash = dash;
    S.server = dash.primary_server || S.server;
    if (view) S.view = view;
    S.dirty = changed;
    updateProfile();
    const active = document.activeElement,
      editing =
        active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
    if (
      (force || changed) &&
      $("modalWrap").classList.contains("hidden") &&
      (force || !editing)
    ) {
      S.dirty = false;
      if (S.page === "project") renderProject();
      else if (S.page === "dashboard") renderDashboard();
      else if (S.page === "account") renderAccount();
      else if (S.page === "transfers") renderTransfers();
      else if (force) await renderSettings();
    }
  } catch (e) {
    if (force) throw e;
    else
      notify(
        t(
          `连接暂时不可用：${e.message}`,
          `Connection unavailable: ${e.message}`,
        ),
        true,
      );
  } finally {
    polling = false;
    if (eventRefreshPending && !eventRefreshTimer)
      eventRefreshTimer = setTimeout(drainEventRefresh, 0);
  }
}
async function boot() {
  try {
    // The language applies before the first authenticated or sign-in view.
    try {
      applyInterfaceLanguage(await api("interface-language"));
    } catch {
      // Keep the current language if the native bridge cannot report it.
    }
    const status = await api("local-status");
    S.profiles = status.profiles || [];
    S.server = status.server || "";
    S.autologin = status.autologin || null;
    S.autologinFailed ||= status.autologin_failed === true;
    if (status.unlocked) await enter();
    // One local account goes straight to its unlock form, so the remember-me
    // checkbox is the first thing on screen instead of one click away.
    else renderAuth(S.profiles.length === 1 ? "login" : "home");
  } catch (e) {
    $("login").replaceChildren(
      el("h1", "", t("正在连接应用…", "Connecting to the app…")),
      el("p", "error", e.message),
      button(t("重试", "Retry"), boot),
      button(t("完全退出", "Quit"), requestAppClose, "link"),
    );
  }
}
$("quitApp").onclick = requestAppClose;
$("dashboardNav").onclick = () => renderDashboard();
$("transfersNav").onclick = () => run(renderTransfers);
$("settingsNav").onclick = () => run(renderSettings);
$("accountNav").onclick = renderAccount;
$("logout").onclick = () =>
  run(async () => {
    S.epoch++;
    S.dash = null;
    await api("logout");
    S.dash = null;
    S.view = null;
    S.drafts.clear();
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
    await boot();
  });
$("closeModal").onclick = closeModal;
$("modalWrap").onclick = (e) => {
  if (e.target === $("modalWrap")) closeModal();
};
document.addEventListener("keydown", handleModalKeyboard);
applyInterfaceLanguage();
window.addEventListener("pywebviewready", boot);
if (window.pywebview?.api) boot();
else
  $("login").append(
    el("h1", "startup-title", t("协作工作台", "Workspace")),
    el(
      "p",
      "muted",
      t(
        "正在连接本机应用。此页面需要在桌面应用中运行。",
        "Connecting to the local app. Open this page in the desktop app.",
      ),
    ),
  );
setInterval(() => {
  if (S.page === "project" && !document.hidden) refresh();
}, 30000);
setInterval(() => {
  if (S.page !== "project" && !document.hidden) refresh();
}, 60000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh(true);
});

FoolshipDesktop.events.refresh = refresh;
FoolshipDesktop.events.boot = boot;
