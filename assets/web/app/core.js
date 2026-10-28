"use strict";
// Keep the desktop page's public surface in one namespace.  The page still
// uses classic scripts for file:// WebView compatibility, but shared state and
// services no longer need to be discovered as anonymous globals.
const HarnessDesktop = ((typeof window !== "undefined"
  ? window
  : globalThis
).HarnessDesktop = (typeof window !== "undefined" ? window : globalThis)
  .HarnessDesktop || {
  state: {},
  api: {},
  render: {},
  events: {},
  components: {},
});
const $ = (id) => document.getElementById(id);
const S = (HarnessDesktop.state = {
  page: "dashboard",
  dash: null,
  view: null,
  project: null,
  profiles: [],
  server: "",
  filter: "all",
  drafts: new Map(),
  streamTexts: new Map(),
  authMode: "login",
  request: null,
  epoch: 0,
});
let toastTimer,
  polling = false,
  quitDialog = null,
  streamTimer = null;
function el(tag, cls = "", text = "") {
  const n = document.createElement(tag);
  n.className = cls;
  n.textContent = text;
  return n;
}

function button(text, fn, cls = "") {
  const b = el("button", cls, text);
  b.type = "button";
  if (fn) b.onclick = () => run(fn, b);
  return b;
}
async function run(fn, b) {
  if (b) b.disabled = true;
  try {
    return await fn();
  } catch (e) {
    notify(e.message, true);
  } finally {
    if (b) b.disabled = false;
  }
}
async function api(action, data = {}) {
  const bridge = window.pywebview?.api;
  if (!bridge?.invoke)
    throw Error("应用连接尚未就绪，请在桌面应用中打开此页面。");
  const reply = await bridge.invoke(action, data);
  if (!reply?.ok) throw Error(reply?.error || "操作失败");
  return reply.result;
}
HarnessDesktop.api.invoke = api;
function notify(text, error = false) {
  $("toast").textContent = text;
  $("toast").classList.remove("hidden");
  $("toast").style.background = error ? "#893c43" : "#28354b";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 6500);
}
function date(value) {
  if (!value) return "尚未进入";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString("zh-CN");
}
function fileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024)
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}
function attachmentCard(file, project) {
  const card = el("div", "note attachment-card");
  card.append(el("span", "", "📎 " + file.name + " · " + fileSize(file.size)));
  if (
    ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(file.mime)
  ) {
    card.append(
      button("预览图片", async () => {
        const result = await api("attachment-get", {
          project,
          id: file.id,
        });
        if (
          !["image/png", "image/jpeg", "image/gif", "image/webp"].includes(
            result.mime,
          )
        )
          throw new Error("附件不是可预览的图片");
        modal(file.name, (body) => {
          const img = el("img");
          img.alt = file.name;
          img.src = "data:" + result.mime + ";base64," + result.base64;
          img.style.maxWidth = "100%";
          img.style.maxHeight = "65vh";
          body.append(img);
        });
      }),
    );
  }
  card.append(
    button("下载", async () => {
      const result = await api("attachment-save", {
        project,
        id: file.id,
      });
      if (result.saved) notify("已保存：" + result.name);
    }),
  );
  return card;
}
function field(parent, label, value = "", options = null, type = "text") {
  const id = "f" + Math.random().toString(36).slice(2),
    l = el("label", "label", label);
  l.htmlFor = id;
  const input = el(options ? "select" : "input");
  input.id = id;
  if (options)
    options.forEach(([v, t]) => {
      const o = el("option", "", t);
      o.value = v;
      input.append(o);
    });
  else input.type = type;
  if (!options || options.some(([v]) => String(v) === String(value ?? "")))
    input.value = value ?? "";
  parent.append(l, input);
  return input;
}
function note(parent, text) {
  parent.append(el("div", "note", text));
}
async function copyText(value, control) {
  if (control?.select) control.select();
  if (navigator.clipboard?.writeText)
    await navigator.clipboard.writeText(value);
  else document.execCommand("copy");
}
function closeModal() {
  if (quitDialog?.busy) return;
  const wasQuit = !!quitDialog;
  quitDialog = null;
  $("modalWrap").classList.add("hidden");
  $("modalBody").replaceChildren();
  $("modalActions").replaceChildren();
  if (wasQuit && S.page === "settings")
    run(() => $("teamServiceCard")?.refresh?.());
}
function modal(title, build) {
  $("modalTitle").textContent = title;
  $("modalBody").replaceChildren();
  $("modalActions").replaceChildren();
  $("modalWrap").classList.remove("hidden");
  build($("modalBody"), $("modalActions"));
}
async function requestAppClose() {
  if (quitDialog) return;
  const state = { busy: false };
  quitDialog = state;
  clearTimeout(teamTimer);
  modal("退出协作工作台", (body, actions) => {
    body.append(
      el(
        "p",
        "",
        "完全退出会停止这台电脑上的任务接收服务，以及应用启动的本机协作服务。",
      ),
    );
    note(
      body,
      "如果本机正在提供协作服务，其他成员会暂时无法连接。账户、项目和文件会保留；已启动的独立作业不会被取消。",
    );
    const detail = el("p", "muted small", "正在检查本机服务…"),
      progress = el("p", "muted small");
    body.append(detail, progress);
    const cancel = button("取消", closeModal),
      background = button("继续后台运行"),
      exit = button("完全退出", null, "primary");
    background.onclick = () => finish("app-background");
    exit.onclick = () => finish("app-exit");
    actions.append(cancel, background, exit);
    cancel.focus();
    async function finish(action) {
      if (state.busy || quitDialog !== state) return;
      state.busy = true;
      [cancel, background, exit, $("closeModal")].forEach(
        (b) => (b.disabled = true),
      );
      progress.className = "muted small";
      progress.textContent =
        action === "app-exit"
          ? "正在停止本机服务，请稍候…"
          : "正在关闭窗口，后台服务继续运行…";
      try {
        await api(action);
      } catch (e) {
        state.busy = false;
        [cancel, background, exit, $("closeModal")].forEach(
          (b) => (b.disabled = false),
        );
        progress.className = "error";
        progress.textContent =
          "未能完成退出：" + e.message + "。窗口已保留，请重试。";
      }
    }
    api("app-services")
      .then((status) => {
        if (quitDialog !== state) return;
        const count = (status.receivers || []).filter(
          (r) => r.running || r.stopping,
        ).length;
        detail.textContent =
          "本机接收服务：" +
          count +
          " 个运行中。" +
          (status.host?.running
            ? "本机协作服务也将停止。"
            : "本机协作服务未运行。");
        if (status.active_jobs)
          body.append(
            el(
              "p",
              "pending small",
              "当前有作业运行。完全退出后，这些作业仍独立运行。",
            ),
          );
      })
      .catch((e) => {
        if (quitDialog === state) {
          detail.className = "error";
          detail.textContent =
            "暂时无法读取服务状态：" +
            e.message +
            "。完全退出时仍会检查是否已安全停止。";
        }
      });
  });
}
window.requestAppClose = requestAppClose;
function head(title, subtitle) {
  const h = el("div", "top"),
    t = el("div");
  t.append(el("h1", "", title), el("p", "muted", subtitle));
  if (title === "控制台" && S.dash?.unavailable_services?.length)
    t.append(
      el("p", "error", "部分协作服务暂时无法连接；其项目会在恢复连接后显示。"),
    );
  h.append(t);
  return h;
}
function empty(parent, text) {
  parent.append(el("div", "empty", text));
}
function nav(page) {
  S.page = page;
  ["dashboard", "transfers", "settings", "account"].forEach((p) =>
    $(p + "Nav").classList.toggle(
      "active",
      p === page || (page === "project" && p === "dashboard"),
    ),
  );
}
function serviceName(server) {
  if (!server) return "本机服务";
  try {
    return new URL(server).host;
  } catch {
    return server;
  }
}
function employeeServer(e) {
  return e.server || S.server;
}
function deviceName(node, server) {
  const d = S.dash?.devices.find(
    (d) =>
      (d.node || d.id) === node &&
      (!server || d.server === server || (!d.server && server === S.server)),
  );
  return d?.name || node;
}
function authHeader(title, subtitle) {
  const root = $("login");
  root.replaceChildren();
  root.append(
    el("div", "logo", "◎ 协作工作台"),
    el("h1", "", title),
    el("p", "muted", subtitle),
  );
  root.append(button("完全退出", requestAppClose, "link"));
  return root;
}
function renderAuth(mode = "home") {
  S.authMode = mode;
  if (mode === "home") {
    const root = authHeader(
      "欢迎使用",
      "先解锁自己的身份，再凭邀请加入不同团队的项目。",
    );
    const choices = el("div", "auth-choices");
    [
      ["login", "登录", "解锁这台电脑上已有的账户"],
      ["register", "创建账户", "第一次使用，从这里开始"],
      ["device", "关联我的另一台电脑", "在新电脑上使用同一个身份"],
      ["recovery", "从恢复包恢复", "原设备不可用时恢复到这台电脑"],
    ].forEach(([m, title, desc]) => {
      const b = button("", () => renderAuth(m), "auth-choice");
      const text = el("span");
      text.append(el("strong", "", title), el("span", "muted small", desc));
      b.append(text, el("span", "auth-arrow", "→"));
      choices.append(b);
    });
    const guide = el("a", "auth-guide", "第一次使用？打开新手教程 →");
    guide.href = "guide.html";
    root.append(choices, guide);
    return;
  }
  const root = authHeader(
    mode === "register"
      ? "创建我的身份"
      : mode === "device"
        ? "关联这台新电脑"
        : mode === "recovery"
          ? "从恢复包恢复"
          : "解锁我的身份",
    mode === "device"
      ? "提交申请后，这台电脑会显示一个关联码。"
      : mode === "recovery"
        ? "选择离线恢复包，为这台电脑生成新的设备身份。"
        : "身份密钥保存在你的电脑上；加入团队项目不用重新注册。",
  );
  const form = el("form");
  root.append(form);
  let user, profile, password, server, name, recoveryPassword;
  if (mode === "login") {
    profile = field(
      form,
      "本机账户",
      "",
      S.profiles.map((p) => [p.id, p.username]),
    );
    if (!S.profiles.length)
      note(
        form,
        "这台电脑还没有账户。返回后选择“创建账户”，或“关联我的另一台电脑”。",
      );
  } else if (mode === "register") user = field(form, "账户名称");
  else if (mode === "device") name = field(form, "这台机器的名称");
  else recoveryPassword = field(form, "恢复包密码", "", null, "password");
  password = field(
    form,
    mode === "login" ? "解锁密码" : "这台电脑的本地解锁密码",
    "",
    null,
    "password",
  );
  password.autocomplete =
    mode === "login" ? "current-password" : "new-password";
  if (mode !== "login") {
    const connection = el(mode === "device" ? "div" : "details");
    if (mode === "register")
      connection.append(el("summary", "label", "连接设置"));
    server = field(connection, "协作服务地址", S.server);
    note(
      connection,
      mode === "device"
        ? "填写已有电脑使用的同一服务的可达地址。127.0.0.1 只代表当前电脑，不能照抄另一台的本机地址。"
        : "这里选择首个协作服务。以后加入其他团队时直接填写项目邀请，无需另建身份。",
    );
    form.append(connection);
  }
  const submit = button(
    mode === "register"
      ? "创建并进入"
      : mode === "device"
        ? "获取新电脑关联码"
        : mode === "recovery"
          ? "选择恢复包并恢复"
          : "登录",
    null,
    "primary",
  );
  submit.type = "submit";
  submit.disabled = mode === "login" && !S.profiles.length;
  form.append(submit);
  form.onsubmit = (e) => {
    e.preventDefault();
    run(async () => {
      if (mode === "device") {
        const r = await api("device-request", {
          server: server.value,
          name: name.value,
          password: password.value,
        });
        password.value = "";
        S.request = r;
        renderPendingDevice(r);
        return;
      }
      if (mode === "recovery") {
        const recovered = await api("recovery-import", {
          server: server.value,
          recovery_password: recoveryPassword.value,
          password: password.value,
        });
        recoveryPassword.value = "";
        password.value = "";
        if (!recovered.imported) {
          notify("已取消选择恢复包");
          return;
        }
        await enter();
        return;
      }
      await api(mode, {
        ...(mode === "login"
          ? { profile: profile.value }
          : { username: user.value, server: server.value }),
        password: password.value,
      });
      password.value = "";
      await enter();
    }, submit);
  };
  note(
    root,
    "密码仅用于本机解锁，与 Codex、OpenAI 或 GitHub 登录无关。项目工作目录不会自动同步。",
  );
  root.append(button("← 返回", () => renderAuth("home"), "auth-back"));
}
function renderPendingDevice(r) {
  const root = authHeader("新电脑关联码", "请在已登录的电脑上完成批准。");
  const code = el("div", "association-code", r.code || r.request_id);
  root.append(el("label", "label", "这台电脑的关联码"), code);
  const steps = el("ol", "association-steps");
  [
    "回到已登录的电脑，点击左下角的账户图标。",
    "点击“输入新电脑的关联码”，输入上面的码，核对名称和设备指纹后批准。",
    "回到这台新电脑，点击下面的“检查批准并完成关联”。",
  ].forEach((text) => steps.append(el("li", "", text)));
  root.append(steps);
  note(
    root,
    "关联码约 10 分钟有效，仅用于这次关联申请。不要把它当作账户密码。",
  );
  root.append(
    button(
      "检查批准并完成关联",
      async () => {
        const claim = await api("device-claim", {
          request_id: r.request_id,
        });
        if (claim.status !== "approved") {
          notify("还未获批准，请在已有设备输入关联码并确认。");
          return;
        }
        S.request = null;
        await enter();
      },
      "primary",
    ),
    button("← 返回", () => renderAuth("device"), "auth-back"),
  );
}
async function enter() {
  const epoch = ++S.epoch;
  const dash = await api("dashboard");
  if (epoch !== S.epoch) return;
  S.dash = dash;
  S.server = dash.primary_server || S.server;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  updateProfile();
  renderDashboard();
}
function updateProfile() {
  const d = S.dash;
  $("profileName").textContent = d.user.username;
  $("avatar").textContent = d.user.username.slice(0, 1);
  $("deviceCount").textContent =
    "同一身份 · " +
    new Set(d.devices.map((x) => x.node || x.id)).size +
    " 台设备";
  $("localName").textContent =
    "当前设备：" + (d.devices.find((x) => x.local)?.name || "本机");
}

Object.assign(HarnessDesktop.components, {
  attachmentCard,
  field,
  modal,
  notify,
});
