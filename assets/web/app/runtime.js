"use strict";
let eventRefreshPending = false;
let eventRefreshTimer = null;

function drainEventRefresh() {
  eventRefreshTimer = null;
  if (!eventRefreshPending) return;
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
  if (!eventRefreshTimer) eventRefreshTimer = setTimeout(drainEventRefresh, 60);
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
      const selected = S.chatSelection?.get(project);
      view = await api("project", {
        id: project,
        visit: false,
        active_conversation_id:
          selected && !selected.startsWith("new:") ? selected : undefined,
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
      else if (force) await renderSettings();
    }
  } catch (e) {
    if (force) throw e;
    else notify("连接暂时不可用：" + e.message, true);
  } finally {
    polling = false;
    if (eventRefreshPending && !eventRefreshTimer)
      eventRefreshTimer = setTimeout(drainEventRefresh, 0);
  }
}
async function boot() {
  try {
    const status = await api("local-status");
    S.profiles = status.profiles || [];
    S.server = status.server || "";
    if (status.unlocked) await enter();
    else renderAuth("home");
  } catch (e) {
    $("login").replaceChildren(
      el("h1", "", "正在连接应用…"),
      el("p", "error", e.message),
      button("重试", boot),
      button("完全退出", requestAppClose, "link"),
    );
  }
}
$("quitApp").onclick = requestAppClose;
$("dashboardNav").onclick = () => renderDashboard();
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
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});
window.addEventListener("pywebviewready", boot);
if (window.pywebview?.api) boot();
else
  $("login").append(
    el("h1", "", "协作工作台"),
    el("p", "muted", "正在连接本机应用。此页面需要在桌面应用中运行。"),
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

HarnessDesktop.events.refresh = refresh;
HarnessDesktop.events.boot = boot;
