"use strict";
// Keep the desktop page's public surface in one namespace.  The page still
// uses classic scripts for file:// WebView compatibility, but shared state and
// services no longer need to be discovered as anonymous globals.
const FoolshipDesktop = ((typeof window !== "undefined"
  ? window
  : globalThis
).FoolshipDesktop = (typeof window !== "undefined" ? window : globalThis)
  .FoolshipDesktop || {
  state: {},
  api: {},
  render: {},
  events: {},
  components: {},
});
const $ = (id) => document.getElementById(id);
const S = (FoolshipDesktop.state = {
  page: "dashboard",
  dash: null,
  view: null,
  project: null,
  profiles: [],
  server: "",
  drafts: new Map(),
  streamTexts: new Map(),
  authMode: "login",
  request: null,
  epoch: 0,
});
let toastTimer,
  polling = false,
  quitDialog = null,
  modalInvoker = null,
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
    throw Error(
      t(
        "应用连接尚未就绪，请在桌面应用中打开此页面。",
        "The app connection is not ready. Open this page in the desktop app.",
      ),
    );
  const reply = await bridge.invoke(action, data);
  if (!reply?.ok) {
    const originalMessage = reply?.error;
    const error = new Error(
      localizeDesktopText(originalMessage) ||
        t("操作失败", "The operation failed."),
    );
    error.originalMessage = originalMessage;
    throw error;
  }
  return reply.result;
}
FoolshipDesktop.api.invoke = api;
function notify(text, error = false) {
  $("toast").textContent = text;
  $("toast").classList.remove("hidden");
  $("toast").style.background = error ? "#893c43" : "#28354b";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 6500);
}
function date(value) {
  if (!value) return t("尚未进入", "Never opened");
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(d.getTime())
    ? String(value)
    : d.toLocaleString(interfaceLocale());
}
function fileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024)
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}
function attachmentKind(file) {
  const mime = String(file.mime || "");
  const name = String(file.name || "");
  if (/^image\/(png|jpeg|gif|webp|bmp)$/.test(mime)) return "image";
  if (/^application\/pdf$/.test(mime)) return "pdf";
  if (
    /^text\//.test(mime) ||
    /^application\/(json|xml|javascript|x-yaml|x-sh|toml)$/.test(mime) ||
    /\.(md|txt|log|csv|tsv|json|ya?ml|toml|ini|cfg|py|js|ts|tsx|jsx|css|html|sh|ps1|bat)$/i.test(
      name,
    )
  )
    return "text";
  return "file";
}

function decodeBase64Text(base64) {
  const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

const ATTACHMENT_DATA = new Map();

/* One fetch per attachment for the whole session.

   Re-rendering the conversation creates new cards constantly; setting `src` from
   a fresh request each time made an image alternate between the broken-image
   icon and the real picture. The promise is cached by project and id, and the
   <img> is only created once a data URL exists.
 */
function attachmentData(project, id) {
  const key = project + ":" + id;
  if (!ATTACHMENT_DATA.has(key)) {
    ATTACHMENT_DATA.set(
      key,
      api("attachment-get", { project, id }).then(
        (result) => "data:" + result.mime + ";base64," + result.base64,
      ),
    );
  }
  return ATTACHMENT_DATA.get(key);
}

function attachmentImage(src, file) {
  const image = el("img", "attachment-thumb");
  image.alt = file.name || t("附件", "Attachment");
  image.title = t("点击查看大图", "View full image");
  image.loading = "lazy";
  image.src = src;
  image.addEventListener("click", () => {
    modal(file.name || t("附件", "Attachment"), (body) => {
      const large = el("img", "attachment-preview-image");
      large.alt = file.name || t("附件", "Attachment");
      large.src = src;
      body.append(large);
    });
  });
  return image;
}

/* A file under a message: preview when the browser can show it, save otherwise.
 *
 * Images are loaded as data URLs (the app's CSP allows `data:` and nothing
 * remote). PDFs are not previewed inside the app: the app shell forbids frames,
 * and opening them there would mean widening that policy for one file type.
 */
function attachmentCard(file, project) {
  const kind = attachmentKind(file);
  const icons = { image: "🖼", text: "📄", pdf: "📕", file: "📦" };
  const card = el("div", "note attachment-card");
  const head = el("div", "attachment-head");
  head.append(
    el("span", "attachment-icon", icons[kind]),
    el("span", "attachment-name", file.name || t("附件", "Attachment")),
    el("span", "muted small", fileSize(file.size || 0)),
  );
  card.append(head);

  const load = () => api("attachment-get", { project, id: file.id });
  const preview = async (build) => {
    const result = await load();
    modal(file.name || t("附件", "Attachment"), (body) => build(body, result));
  };

  if (kind === "image") {
    const slot = el("div", "attachment-slot");
    slot.append(el("div", "attachment-skeleton"));
    card.append(slot);
    attachmentData(project, file.id)
      .then((src) => {
        if (slot.isConnected) slot.replaceChildren(attachmentImage(src, file));
      })
      .catch(() => {
        if (slot.isConnected)
          slot.replaceChildren(
            el(
              "span",
              "muted small",
              t(
                "预览不可用，可下载后查看",
                "Preview unavailable. Download the file to view it.",
              ),
            ),
          );
      });
  }

  const actions = el("div", "attachment-actions");
  if (kind === "text") {
    actions.append(
      button(t("预览", "Preview"), async () => {
        try {
          await preview((body, result) => {
            const text = el("pre", "attachment-preview");
            text.textContent = decodeBase64Text(result.base64);
            body.append(text);
          });
        } catch (error) {
          notify(
            error.message ||
              t("无法打开附件", "Could not open the attachment."),
          );
        }
      }),
    );
  }
  actions.append(
    button(t("下载", "Download"), async () => {
      const result = await api("attachment-save", { project, id: file.id });
      if (result.saved)
        notify(t(`已保存：${result.name}`, `Saved: ${result.name}`));
    }),
  );
  card.append(actions);
  return card;
}
function field(parent, label, value = "", options = null, type = "text") {
  const id = "f" + Math.random().toString(36).slice(2),
    l = el("label", "label", label);
  l.htmlFor = id;
  const input = el(
    options ? "select" : type === "textarea" ? "textarea" : "input",
  );
  input.id = id;
  if (options)
    options.forEach(([v, t]) => {
      const o = el("option", "", t);
      o.value = v;
      input.append(o);
    });
  else if (type === "textarea") input.rows = 2;
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
  if (quitDialog?.busy || $("modalWrap").classList.contains("hidden")) return;
  const wasQuit = !!quitDialog;
  quitDialog = null;
  $("modalWrap").classList.add("hidden");
  $("modalBody").replaceChildren();
  $("modalActions").replaceChildren();
  if (modalInvoker?.isConnected) modalInvoker.focus();
  modalInvoker = null;
  if (wasQuit && S.page === "settings")
    run(() => $("teamServiceCard")?.refresh?.());
}
function modal(title, build) {
  const wrapper = $("modalWrap");
  if (wrapper.classList.contains("hidden"))
    modalInvoker = document.activeElement;
  $("modalTitle").textContent = title;
  $("modalBody").replaceChildren();
  $("modalActions").replaceChildren();
  wrapper.classList.remove("hidden");
  build($("modalBody"), $("modalActions"));
  if (!wrapper.contains(document.activeElement)) {
    const firstField = Array.from(
      $("modalBody").querySelectorAll(
        'input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])',
      ),
    ).find((node) => node.getClientRects().length > 0);
    (firstField || $("closeModal")).focus();
  }
}

function handleModalKeyboard(event) {
  const wrapper = $("modalWrap");
  if (wrapper.classList.contains("hidden")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeModal();
  } else if (event.key === "Tab") {
    const focusable = Array.from(
      wrapper.querySelectorAll(
        'button, a[href], input:not([type="hidden"]), select, textarea, summary, [tabindex]',
      ),
    ).filter(
      (node) =>
        !node.disabled && node.tabIndex >= 0 && node.getClientRects().length,
    );
    if (!focusable.length) return;
    const index = focusable.indexOf(document.activeElement);
    if (
      index < 0 ||
      (event.shiftKey && index === 0) ||
      (!event.shiftKey && index === focusable.length - 1)
    ) {
      event.preventDefault();
      (event.shiftKey ? focusable.at(-1) : focusable[0]).focus();
    }
  }
}
async function requestAppClose() {
  if (quitDialog) return;
  const state = { busy: false };
  quitDialog = state;
  clearTimeout(teamTimer);
  let canBackground = false;
  try {
    canBackground = !!(await api("app-capabilities"))?.background;
  } catch {
    canBackground = false;
  }
  modal(t("退出协作工作台", "Quit Foolship"), (body, actions) => {
    body.append(
      el(
        "p",
        "",
        t(
          "完全退出会停止这台电脑上的任务接收服务，以及应用启动的本机协作服务。",
          "Quitting stops this computer's task receiver and any collaboration service started by the app.",
        ),
      ),
    );
    note(
      body,
      canBackground
        ? t(
            "如果本机正在提供协作服务，其他成员会暂时无法连接。账户、项目和文件会保留；已启动的独立作业不会被取消。选择后台运行时窗口会收起到系统托盘或程序坞，任务接收服务继续运行。",
            "Team members will temporarily lose access if this computer hosts the service. Your account, projects, files and independent jobs are preserved. Keep running in background to hide the window and continue receiving tasks.",
          )
        : t(
            "如果本机正在提供协作服务，其他成员会暂时无法连接。账户、项目和文件会保留；已启动的独立作业不会被取消。这个平台没有托盘图标，关闭窗口即完全退出。",
            "Team members will temporarily lose access if this computer hosts the service. Your account, projects, files and independent jobs are preserved. Closing the window quits the app on this platform.",
          ),
    );
    const detail = el(
        "p",
        "muted small",
        t("正在检查本机服务…", "Checking local services…"),
      ),
      progress = el("p", "muted small");
    body.append(detail, progress);
    const cancel = button(t("取消", "Cancel"), closeModal),
      background = canBackground
        ? button(t("继续后台运行", "Keep running in background"))
        : null,
      exit = button(t("完全退出", "Quit"), null, "primary");
    if (background) background.onclick = () => finish("app-background");
    exit.onclick = () => finish("app-exit");
    actions.append(cancel, ...(background ? [background] : []), exit);
    cancel.focus();
    const stopButtons = [cancel, background, exit, $("closeModal")].filter(
      Boolean,
    );
    async function finish(action) {
      if (state.busy || quitDialog !== state) return;
      state.busy = true;
      stopButtons.forEach((b) => (b.disabled = true));
      progress.className = "muted small";
      progress.textContent =
        action === "app-exit"
          ? t("正在停止本机服务，请稍候…", "Stopping local services…")
          : t(
              "正在关闭窗口，后台服务继续运行…",
              "Hiding the window. Services will keep running…",
            );
      try {
        await api(action);
        if (action === "app-background") {
          // The window is only hidden, so this dialog would otherwise survive
          // the trip to the tray and sit there with every button disabled.
          state.busy = false;
          closeModal();
        }
      } catch (e) {
        state.busy = false;
        stopButtons.forEach((b) => (b.disabled = false));
        progress.className = "error";
        progress.textContent = t(
          `未能完成退出：${e.message}。窗口已保留，请重试。`,
          `Could not quit: ${e.message} The window is still open. Try again.`,
        );
      }
    }
    api("app-services")
      .then((status) => {
        if (quitDialog !== state) return;
        const count = (status.receivers || []).filter(
          (r) => r.running || r.stopping,
        ).length;
        detail.textContent =
          t(
            `本机接收服务：${count} 个运行中。`,
            `Local task receivers running: ${count}. `,
          ) +
          (status.host?.running
            ? t(
                "本机协作服务也将停止。",
                "The local collaboration service will also stop.",
              )
            : t(
                "本机协作服务未运行。",
                "The local collaboration service is stopped.",
              ));
        if (status.active_jobs)
          body.append(
            el(
              "p",
              "pending small",
              t(
                "当前有作业运行。完全退出后，这些作业仍独立运行。",
                "Some jobs are running. They will continue independently after you quit.",
              ),
            ),
          );
      })
      .catch((e) => {
        if (quitDialog === state) {
          detail.className = "error";
          detail.textContent = t(
            `暂时无法读取服务状态：${e.message}。完全退出时仍会检查是否已安全停止。`,
            `Could not check service status: ${e.message} Foolship will check that services stop safely when you quit.`,
          );
        }
      });
  });
}
window.requestAppClose = requestAppClose;
function head(title, subtitle) {
  const h = el("div", "top"),
    t = el("div");
  t.append(el("h1", "", title));
  if (subtitle) t.append(el("p", "muted", subtitle));
  h.append(t);
  return h;
}
function empty(parent, text) {
  parent.append(el("div", "empty", text));
}
function nav(page) {
  S.page = page;
  ["dashboard", "transfers", "settings", "account"].forEach((p) => {
    const target = $(p + "Nav");
    const active = p === page || (page === "project" && p === "dashboard");
    target.classList.toggle("active", active);
    if (active) target.setAttribute("aria-current", "page");
    else target.removeAttribute("aria-current");
  });
}
function serviceName(server) {
  if (!server) return t("本机服务", "Local service");
  try {
    return new URL(server).host;
  } catch {
    return server;
  }
}
function deviceName(node, server) {
  const d = S.dash?.devices.find(
    (d) =>
      (d.node || d.id) === node &&
      (!server || d.server === server || (!d.server && server === S.server)),
  );
  const suffix = String(node || "")
    .replace(/^device-/, "")
    .slice(0, 8);
  return d?.name || t(`设备 ${suffix}`, `Device ${suffix}`);
}
function authHeader(title, subtitle) {
  const root = $("login");
  root.replaceChildren();
  const brand = el("div", "logo auth-card-title");
  const mark = el("span", "logo-icon", "◎");
  mark.setAttribute("aria-hidden", "true");
  brand.append(mark, el("span", "", "Foolship"));
  root.append(brand, el("h1", "", title));
  if (subtitle) root.append(el("p", "muted", subtitle));
  if (S.autologinFailed && ["home", "login"].includes(S.authMode)) {
    const notice = el(
      "p",
      "error",
      t(
        "自动登录未成功，请手动解锁本机账户。",
        "Automatic sign-in failed. Unlock your local account to continue.",
      ),
    );
    notice.setAttribute("role", "status");
    root.append(notice);
  }
  return root;
}
function renderAuth(mode = "home") {
  S.authMode = mode;
  if (mode === "home") {
    const root = authHeader(t("欢迎使用", "Welcome"));
    const choices = el("div", "auth-choices");
    [
      [
        "login",
        t("登录", "Sign in"),
        t("解锁这台电脑上已有的账户", "Unlock an account on this computer"),
      ],
      [
        "register",
        t("创建账户", "Create account"),
        t("第一次使用，从这里开始", "Set up your first account"),
      ],
      [
        "device",
        t("关联我的另一台电脑", "Link another computer"),
        t("在新电脑上使用同一个身份", "Use the same account on a new computer"),
      ],
      [
        "recovery",
        t("从恢复包恢复", "Restore from a recovery file"),
        t(
          "原设备不可用时恢复到这台电脑",
          "Recover your account if the original device is unavailable",
        ),
      ],
    ].forEach(([m, title, desc]) => {
      const b = button("", () => renderAuth(m), "auth-choice");
      const text = el("span");
      text.append(el("strong", "", title), el("span", "muted small", desc));
      const arrow = el("span", "auth-arrow", "→");
      arrow.setAttribute("aria-hidden", "true");
      b.append(text, arrow);
      choices.append(b);
    });
    const guide = el(
      "a",
      "auth-guide",
      t("第一次使用？打开新手教程 →", "Getting started guide →"),
    );
    guide.href = t("guide.html", "guide-en.html");
    const foot = el("div", "auth-foot");
    const select = el("select", "auth-language");
    select.id = "authLanguage";
    select.setAttribute("aria-label", t("界面语言", "Display language"));
    for (const [value, label] of languagePreferenceOptions()) {
      const option = el("option", "", label);
      option.value = value;
      select.append(option);
    }
    select.value = interfaceLanguagePreference();
    select.onchange = () =>
      run(async () => {
        try {
          await setInterfaceLanguage(select.value);
          renderAuth("home");
          $("authLanguage").focus();
        } catch (error) {
          select.value = interfaceLanguagePreference();
          throw error;
        }
      }, select);
    foot.append(guide, select);
    root.append(choices, foot);
    return;
  }
  const root = authHeader(
    mode === "register"
      ? t("创建我的身份", "Create your account")
      : mode === "device"
        ? t("关联这台新电脑", "Link this computer")
        : mode === "recovery"
          ? t("从恢复包恢复", "Restore from a recovery file")
          : t("解锁我的身份", "Unlock your account"),
    mode === "device"
      ? t(
          "提交申请后，这台电脑会显示一个关联码。",
          "A link code will appear on this computer after you submit the request.",
        )
      : mode === "recovery"
        ? t(
            "选择离线恢复包，为这台电脑生成新的设备身份。",
            "Choose a recovery file to set up this computer as a new device.",
          )
        : "",
  );
  const form = el("form");
  root.append(form);
  let user, profile, password, server, name, recoveryPassword;
  if (mode === "login") {
    profile = field(
      form,
      t("本机账户", "Local account"),
      "",
      S.profiles.map((p) => [p.id, p.username]),
    );
    if (!S.profiles.length)
      note(
        form,
        t(
          "这台电脑还没有账户。返回后选择“创建账户”，或“关联我的另一台电脑”。",
          "This computer has no account yet. Go back to create an account or link another computer.",
        ),
      );
  } else if (mode === "register") {
    user = field(form, t("账户名称", "Account name"));
    name = field(
      form,
      t("这台机器的名称（留空用系统主机名）", "Computer name (optional)"),
    );
  } else if (mode === "device")
    name = field(form, t("这台机器的名称", "Computer name"));
  else
    recoveryPassword = field(
      form,
      t("恢复包密码", "Recovery file password"),
      "",
      null,
      "password",
    );
  password = field(
    form,
    mode === "login"
      ? t("解锁密码", "Unlock password")
      : t("这台电脑的本地解锁密码", "Unlock password for this computer"),
    "",
    null,
    "password",
  );
  password.autocomplete =
    mode === "login" ? "current-password" : "new-password";
  // Remembering is per account and follows the selected profile.
  let remember = null;
  if (mode === "login") {
    const box = el("input");
    box.type = "checkbox";
    box.checked = !!(profile.value && S.autologin === profile.value);
    const inline = el("label", "remember-line");
    inline.append(
      box,
      el("span", "", t("下次自动登录", "Sign in automatically")),
    );
    form.append(inline);
    profile.onchange = () => {
      box.checked = !!(profile.value && S.autologin === profile.value);
    };
    remember = box;
  }
  if (mode !== "login") {
    const connection = el(mode === "device" ? "div" : "details");
    if (mode === "register")
      connection.append(
        el("summary", "label", t("连接设置", "Connection settings")),
      );
    server = field(
      connection,
      t("协作服务地址", "Collaboration service address"),
      mode === "device" ? "" : S.server,
    );
    if (mode === "device") {
      server.placeholder = t(
        "https://电脑名.xxxx.ts.net",
        "https://computer.example.ts.net",
      );
      server.autocapitalize = "off";
      server.spellcheck = false;
    }
    note(
      connection,
      mode === "device"
        ? t(
            "填写已有电脑使用的同一服务的可达地址。127.0.0.1 只代表当前电脑，不能照抄另一台的本机地址。",
            "Enter a reachable address for the same service your other computer uses. The address 127.0.0.1 points to this computer.",
          )
        : t(
            "这里选择首个协作服务。以后加入其他团队时直接填写项目邀请，无需另建身份。",
            "Choose your first collaboration service. You can join other teams later using project invites and the same account.",
          ),
    );
    form.append(connection);
  }
  const submit = button(
    mode === "register"
      ? t("创建并进入", "Create and open")
      : mode === "device"
        ? t("获取新电脑关联码", "Get link code")
        : mode === "recovery"
          ? t("选择恢复包并恢复", "Choose recovery file")
          : t("登录", "Sign in"),
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
        const address = server.value.trim();
        if (!address)
          throw new Error(
            t(
              "请填写已有电脑的团队地址，例如 https://电脑名.xxxx.ts.net",
              "Enter a reachable address for the other computer, such as https://computer.example.ts.net.",
            ),
          );
        if (
          /^(https?:\/\/)?(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/i.test(
            address,
          )
        )
          throw new Error(
            t(
              "127.0.0.1 指的是当前这台电脑自己，另一台电脑找不到它；请填写已有电脑的团队地址（https://…）",
              "The address 127.0.0.1 points to this computer. Enter the other computer’s reachable service address.",
            ),
          );
        const r = await api("device-request", {
          server: address,
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
          notify(t("已取消选择恢复包", "Recovery file selection canceled."));
          return;
        }
        await enter();
        return;
      }
      await api(mode, {
        ...(mode === "login"
          ? { profile: profile.value }
          : {
              username: user.value,
              server: server.value,
              ...(mode === "register" && name?.value.trim()
                ? { device_name: name.value.trim() }
                : {}),
            }),
        password: password.value,
        ...(mode === "login" ? { remember: Boolean(remember?.checked) } : {}),
      });
      password.value = "";
      await enter();
    }, submit);
  };
  const foot = el("div", "auth-foot");
  foot.append(
    button(t("← 返回", "← Back"), () => renderAuth("home"), "auth-back"),
    button(t("完全退出", "Quit"), requestAppClose, "link"),
  );
  root.append(foot);
}
function renderPendingDevice(r) {
  const root = authHeader(
    t("新电脑关联码", "Device link code"),
    t(
      "请在已登录的电脑上完成批准。",
      "Approve this request on a computer where you’re signed in.",
    ),
  );
  const code = el("div", "association-code", r.code || r.request_id);
  root.append(
    el("label", "label", t("这台电脑的关联码", "This computer’s link code")),
    code,
  );
  const steps = el("ol", "association-steps");
  [
    t(
      "回到已登录的电脑，点击左下角的账户图标。",
      "Open Account on the computer where you’re signed in.",
    ),
    t(
      "点击“输入新电脑的关联码”，输入上面的码，核对名称和设备指纹后批准。",
      "Choose Enter link code. Enter the code above, verify the name and device fingerprint, then approve.",
    ),
    t(
      "回到这台新电脑，点击下面的“检查批准并完成关联”。",
      "Return to this computer and choose Check approval and link.",
    ),
  ].forEach((text) => steps.append(el("li", "", text)));
  root.append(steps);
  note(
    root,
    t(
      "关联码约 10 分钟有效，仅用于这次关联申请。不要把它当作账户密码。",
      "The code expires in about 10 minutes and is only valid for this request.",
    ),
  );
  root.append(
    button(
      t("检查批准并完成关联", "Check approval and link"),
      async () => {
        const claim = await api("device-claim", {
          request_id: r.request_id,
        });
        if (claim.status !== "approved") {
          notify(
            t(
              "还未获批准，请在已有设备输入关联码并确认。",
              "Approval is pending. Enter the code on your existing device and approve the request.",
            ),
          );
          return;
        }
        S.request = null;
        await enter();
      },
      "primary",
    ),
    button(t("← 返回", "← Back"), () => renderAuth("device"), "auth-back"),
  );
}
async function enter() {
  const epoch = ++S.epoch;
  const dash = await api("dashboard");
  if (epoch !== S.epoch) return;
  S.autologinFailed = false;
  S.dash = dash;
  S.server = dash.primary_server || S.server;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  updateProfile();
  renderDashboard();
  // Fire and forget: a due release check must never delay the first paint.
  api("update-status")
    .then((state) => {
      if (state?.status === "update")
        notify(
          t(
            `有新版本 ${state.latest}；可在 设置 → 应用更新 中打开下载页。`,
            `Version ${state.latest} is available. Open Settings → App updates to download it.`,
          ),
        );
    })
    .catch(() => {});
}
function updateProfile() {
  const d = S.dash;
  $("profileName").textContent = d.user.username;
  $("avatar").textContent = d.user.username.slice(0, 1);
  const count = new Set(d.devices.map((x) => x.node || x.id)).size;
  $("deviceCount").textContent = t(
    `同一身份 · ${count} 台设备`,
    `Same account · ${count} ${count === 1 ? "device" : "devices"}`,
  );
  const localDevice =
    d.devices.find((x) => x.local)?.name || t("本机", "This computer");
  $("localName").textContent = t(
    `当前设备：${localDevice}`,
    `Current device: ${localDevice}`,
  );
}

Object.assign(FoolshipDesktop.components, {
  attachmentCard,
  field,
  modal,
  notify,
});
