"use strict";
function renderAccount() {
  S.epoch++;
  nav("account");
  const c = $("content");
  c.replaceChildren(head("账户", "管理同一身份下的设备和本机登录。"));
  const list = el("div", "settings");
  const account = el("section", "panel");
  account.append(
    el("h2", "", "身份与关联设备"),
    el("p", "muted", S.dash.user.username),
  );
  S.dash.devices
    .filter((d) => !d.server || d.server === S.server)
    .forEach((d) => {
      const row = el("div", "setting-row");
      row.append(
        el(
          "div",
          "",
          d.name +
            (d.local ? " · 当前设备" : "") +
            " · " +
            (d.online ? "在线" : "离线"),
        ),
        button("移除设备", () =>
          modal("移除设备", (b, a) => {
            note(b, "将撤销 " + d.name + " 的账户接入权限。");
            a.append(
              button("取消", closeModal),
              button(
                "确认移除",
                async () => {
                  await api("device-revoke", {
                    id: d.id || d.node,
                    server: d.server || S.server,
                  });
                  closeModal();
                  await refresh(true);
                },
                "primary",
              ),
            );
          }),
        ),
      );
      if (d.is_root) {
        row.querySelector("button").disabled = true;
        row.querySelector("button").textContent = "根设备 · 保留";
      }
      account.append(row);
    });
  account.append(
    button("输入新电脑的关联码", () =>
      modal("关联新电脑", (b, a) => {
        const code = field(b, "新电脑显示的关联码");
        a.append(
          button("取消", closeModal),
          button(
            "核对请求",
            async () => {
              await api("device-inspect", { code: code.value.trim() });
              closeModal();
              await refresh(true);
            },
            "primary",
          ),
        );
      }),
    ),
  );
  account.append(button("在另一台电脑上安装", aiInstallSetup));
  account.append(
    button("创建离线恢复包", () =>
      modal("创建离线恢复包", (b, a) => {
        note(
          b,
          "恢复包只适用于根设备。它可在原设备不可用时恢复账户，请离线保存并使用独立密码保护。",
        );
        const password = field(b, "恢复包密码", "", null, "password");
        const confirm = field(b, "再次输入", "", null, "password");
        a.append(
          button("取消", closeModal),
          button(
            "选择保存位置",
            async () => {
              if (!password.value || password.value !== confirm.value)
                throw new Error("两次输入的恢复包密码不一致");
              const result = await api("recovery-export", {
                recovery_password: password.value,
              });
              password.value = "";
              confirm.value = "";
              if (result.saved) {
                closeModal();
                notify("恢复包已保存：" + result.name);
              }
            },
            "primary",
          ),
        );
      }),
    ),
  );
  note(
    account,
    "关联新电脑：在新电脑的登录页选择“关联我的另一台电脑”，点“输入新电脑的关联码”，核对并批准请求。",
  );
  (S.dash.pending_devices || []).forEach((d) => {
    const r = el("div", "setting-row");
    r.append(
      el("div", "", d.name + " · 等待批准"),
      button("核对并批准", () =>
        modal("关联设备确认", (b, a) => {
          note(b, "仅批准你自己发起的请求。");
          b.append(
            el("p", "", "名称：" + d.name),
            el("p", "", "设备指纹：" + d.public_key_fingerprint),
            el("p", "", "有效期：" + date(d.expires)),
          );
          a.append(
            button("取消", closeModal),
            button(
              "批准关联",
              async () => {
                await api("device-approve", { request_id: d.id });
                closeModal();
                await refresh(true);
              },
              "primary",
            ),
          );
        }),
      ),
    );
    account.append(r);
  });
  list.append(account);
  const actions = el("section", "panel");
  actions.append(el("h2", "", "本机账户"));
  actions.append(
    el(
      "p",
      "muted small",
      S.dash.user.username + " · " + $("deviceCount").textContent,
    ),
  );
  actions.append(el("p", "muted small", $("localName").textContent));
  const row = el("div", "account-actions");
  row.append(
    button("退出账户", () => $("logout").click()),
    button("完全退出应用", () => $("quitApp").click()),
  );
  actions.append(row);
  list.append(actions);
  c.append(list);
  renderApprovals(c);
}

async function renderSettings() {
  nav("settings");
  const settings = await api("settings-get");
  if (S.page !== "settings") return;
  const c = $("content");
  c.replaceChildren(head("设置", "调整这台机器的连接、Agent 与运行偏好。"));
  const list = el("div", "settings");
  const team = teamServiceCard(settings.team_service);
  list.append(team);
  const machine = el("section", "panel");
  machine.append(el("h2", "", "本机状态"));
  note(
    machine,
    "接收服务在这台机器上后台运行：领取派给这台机器的任务，也接收发来的文件。登录时自动启动；停止它不会退出应用，但任务会停在服务端等你重新启动。",
  );
  const receivers = Array.isArray(settings.receivers) ? settings.receivers : [];
  const services = settings.services || [
    { server: settings.server, primary: true, attached: true },
  ];
  services.forEach((service) => {
    const receiver = receivers.find((item) => item.server === service.server);
    const state = !service.attached
      ? "未接入这台机器"
      : receiver?.running
        ? receiver.online
          ? "运行中 · 已连接"
          : "运行中 · 未连接"
        : "已停止";
    const row = el("div", "setting-row");
    row.append(
      el(
        "div",
        "",
        serviceName(service.server) +
          (service.primary ? " · 首个服务" : "") +
          " · " +
          state,
      ),
    );
    if (!service.attached)
      row.append(
        button("接入这台机器", () =>
          modal("接入这台机器", (b, a) => {
            note(
              b,
              "接入后可以在 " +
                serviceName(service.server) +
                " 上创建和派遣本机员工。接入只影响这台机器，不改动其它电脑。",
            );
            a.append(
              button("取消", closeModal),
              button(
                "确认接入",
                async () => {
                  await api("receiver-attach", { server: service.server });
                  closeModal();
                  notify("本机已接入 " + serviceName(service.server));
                  await refresh(true);
                },
                "primary",
              ),
            );
          }),
        ),
      );
    machine.append(row);
  });
  const running = receivers.filter((item) => item.running);
  const controls = el("div", "setting-row");
  const startReceiver = button(
    "启动接收",
    async () => {
      await api("receiver-start");
      notify("本机接收服务已启动");
      await refresh(true);
    },
    "primary",
  );
  const stopReceiver = button("停止接收", async () => {
    await api("receiver-stop");
    notify("接收服务已停止；已启动的作业独立运行。");
    await refresh(true);
  });
  controls.append(startReceiver, stopReceiver);
  machine.append(controls, button("完全退出", requestAppClose));
  const attached = services.filter((service) => service.attached);
  startReceiver.disabled =
    attached.length > 0 && running.length === attached.length;
  stopReceiver.disabled = running.length === 0;
  list.append(
    machine,
    executorCard(settings.executor),
    proxySettingsCard(settings.proxy),
    idleSettingsCard(settings.warm_idle_seconds),
    codexCliCard("codex"),
    codexCliCard("dsh"),
    codexMcpCard(settings.codex_mcp),
    dshMcpCard(settings.dsh_mcp),
  );
  list.append(updateCard());
  const diagnostics = el("section", "panel");
  diagnostics.append(
    el("h2", "", "安全诊断"),
    el(
      "p",
      "muted small",
      "生成可分享的最小诊断信息；不包含账户、设备、项目、消息、路径、服务地址、代理或日志原文。",
    ),
    button("查看安全诊断", () =>
      run(async () => {
        const snapshot = await api("diagnostics-get");
        modal("安全诊断", (body, actions) => {
          const output = document.createElement("textarea");
          output.readOnly = true;
          output.rows = 18;
          output.value = JSON.stringify(snapshot, null, 2);
          body.append(output);
          actions.append(
            button("关闭", closeModal),
            button(
              "复制",
              async () => {
                await copyText(output.value, output);
                notify("安全诊断已复制");
              },
              "primary",
            ),
          );
        });
      }),
    ),
  );
  list.append(diagnostics);
  const advanced = el("details", "panel");
  advanced.append(el("summary", "", "高级 · 服务连接"));
  const server = field(advanced, "协作服务地址", settings.server);
  server.readOnly = true;
  note(
    advanced,
    "这是创建身份时接入的第一个服务。要加入别处的项目，用那个项目发来的邀请。",
  );
  list.append(advanced);
  c.append(list);
  renderApprovals(c);
  if (settings.team_service) team.paint(settings.team_service);
  else await team.refresh();
}

async function aiInstallSetup() {
  let services = (S.dash.services || []).filter((item) =>
    String(item.server || "").startsWith("https://"),
  );
  try {
    const settings = await api("settings-get");
    const hosted = settings?.team_service;
    if (
      hosted?.state === "ready" &&
      String(hosted.url || "").startsWith("https://") &&
      !services.some((item) => item.server === hosted.url)
    )
      services = [...services, { server: hosted.url, hosted_by_me: true }];
  } catch {
    // Without the settings payload the dashboard list is still worth offering.
  }
  const platforms = [
    ["linux", "Linux 服务器（无界面接收端）"],
    ["windows", "Windows 电脑"],
    ["macos", "macOS 电脑（Apple Silicon）"],
  ];
  modal("用 AI 在另一台电脑上安装", (body, actions) => {
    note(
      body,
      "把这段提示词交给那台机器上的 AI。它负责检查环境、下载、校验和安装；登录与设备批准仍由你在那台机器上完成。",
    );
    if (!services.length) {
      note(
        body,
        "当前账户还没有可供另一台机器访问的 HTTPS 团队入口。请先在设置 → 协作服务中创建团队入口。",
      );
      actions.append(button("关闭", closeModal));
      return;
    }
    const platform = field(body, "目标机器", "linux", platforms);
    const service = field(
      body,
      "接入的协作服务",
      services[0].server,
      services.map((item) => [item.server, serviceName(item.server)]),
    );
    const output = document.createElement("textarea");
    output.readOnly = true;
    output.rows = 18;
    output.placeholder = "正在生成提示词…";
    body.append(output);
    const copy = button(
      "复制给那台机器的 AI",
      async () => {
        if (!output.value) throw new Error("提示词尚未生成");
        await copyText(output.value, output);
        notify("安装提示词已复制");
      },
      "primary",
    );
    copy.disabled = true;
    async function generate() {
      copy.disabled = true;
      output.value = "";
      const result = await api("ai-setup-prompt", {
        server: service.value,
        platform: platform.value,
      });
      output.value = result.prompt;
      copy.disabled = false;
    }
    platform.onchange = () => run(generate);
    service.onchange = () => run(generate);
    actions.append(button("关闭", closeModal), copy);
    run(generate);
  });
}

function updateCard() {
  const card = el("section", "panel");
  card.append(el("h2", "", "应用更新"));
  note(
    card,
    "只向本仓库的 Releases 查版本。可以后台预下载并校验，安装仍要你点一次；没有网络时不检查，选“从不”就完全关闭。",
  );
  const schedule = field(card, "检查频率", "weekly", [
    ["daily", "每天"],
    ["weekly", "每周"],
    ["monthly", "每月"],
    ["never", "从不"],
  ]);
  schedule.id = "updateSchedule";
  const statusLine = el("p", "muted small", "正在读取更新状态…");
  statusLine.style.overflowWrap = "anywhere";
  const actions = el("div", "setting-row");
  const check = button("立即检查", async () => {
    check.disabled = true;
    try {
      paint(await api("update-status", { force: true }));
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = "检查失败：" + e.message;
    } finally {
      check.disabled = false;
    }
  });
  const fetchButton = button(
    "下载更新",
    async () => {
      fetchButton.disabled = true;
      try {
        paintDownload(await api("update-download"));
        pollDownload();
      } catch (e) {
        notify("开始下载失败：" + e.message, true);
        fetchButton.disabled = false;
      }
    },
    "primary",
  );
  const install = button(
    "安装并重启",
    async () => {
      await begin("update-apply", false);
    },
    "primary",
  );
  const installAnyway = button("仍然安装并重启", async () => {
    await begin("update-apply", true);
  });
  const rollback = button("回到上一版本", async () => {
    await begin("update-rollback", true);
  });
  const discard = button("删除已下载的更新", async () => {
    await api("update-discard");
    paintDownload({ status: "idle" });
    notify("已删除下载的安装包。");
  });
  const open = button("打开下载页", async () => {
    await api("update-open");
  });
  for (const item of [
    fetchButton,
    install,
    installAnyway,
    rollback,
    discard,
    open,
  ])
    item.classList.add("hidden");
  actions.append(
    check,
    fetchButton,
    install,
    installAnyway,
    rollback,
    discard,
    open,
  );
  card.append(schedule, statusLine, actions);

  async function begin(action, force) {
    for (const item of [install, installAnyway, rollback]) item.disabled = true;
    try {
      const result = await api(action, { force });
      notify(
        "正在退出并" +
          (action === "update-rollback" ? "回退" : "安装") +
          "到 " +
          result.version +
          "，应用会自动重启。",
      );
      await api("app-exit");
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = e.message;
      installAnyway.classList.toggle(
        "hidden",
        !String(e.message).includes("会中断"),
      );
    } finally {
      install.disabled = false;
      rollback.disabled = false;
    }
  }

  let latest = "";
  let poller = null;

  function megabytes(bytes) {
    return (Number(bytes || 0) / 1048576).toFixed(1) + " MB";
  }

  function paintDownload(state) {
    const d = state || {};
    const running = d.status === "downloading" || d.status === "verifying";
    fetchButton.classList.toggle("hidden", running || d.status === "ready");
    fetchButton.disabled = running;
    discard.classList.toggle("hidden", d.status !== "ready");
    open.classList.remove("hidden");
    if (running) {
      const total = d.total ? megabytes(d.total) : "未知大小";
      const percent = d.total
        ? Math.min(100, Math.round((Number(d.received || 0) / d.total) * 100))
        : null;
      statusLine.className = "small";
      statusLine.textContent =
        "正在下载 " +
        megabytes(d.received) +
        " / " +
        total +
        (percent === null ? "" : "（" + percent + "%）") +
        "；安装仍需你确认，不会自动替换。";
    } else if (d.status === "ready") {
      statusLine.className = "small";
      statusLine.textContent =
        "已下载 v" +
        (d.version || latest) +
        "（" +
        megabytes(d.total) +
        "），SHA-256 已校验。安装会先退出应用，替换完成后自动重启。";
      fetchButton.classList.add("hidden");
      install.classList.remove("hidden");
      discard.classList.remove("hidden");
      if (poller) {
        clearInterval(poller);
        poller = null;
      }
    } else if (d.status === "failed") {
      statusLine.className = "error small";
      statusLine.textContent = "下载失败：" + (d.error || "未知错误");
      fetchButton.classList.remove("hidden");
      fetchButton.disabled = false;
      if (poller) {
        clearInterval(poller);
        poller = null;
      }
    }
  }

  function pollDownload() {
    if (poller) return;
    poller = setInterval(async () => {
      try {
        paintDownload(await api("update-download-status"));
      } catch (e) {
        clearInterval(poller);
        poller = null;
      }
    }, 1000);
  }

  function paint(state) {
    const s = state || {};
    if (s.schedule) schedule.value = s.schedule;
    latest = s.latest || latest;
    open.classList.toggle("hidden", s.status !== "update");
    if (s.status === "update") {
      statusLine.className = "small";
      statusLine.textContent =
        "有新版本 " + (s.latest || "") + "（当前 " + s.current + "）。";
      fetchButton.classList.remove("hidden");
    } else if (s.status === "current") {
      statusLine.className = "muted small";
      statusLine.textContent = "已是最新版本 " + s.current + "。";
      fetchButton.classList.add("hidden");
    } else if (s.status === "offline") {
      statusLine.className = "muted small";
      statusLine.textContent = "暂时连不上 GitHub；稍后会自动再试。";
    } else if (s.status === "disabled") {
      statusLine.className = "muted small";
      statusLine.textContent = "已关闭自动检查。仍然可以点“立即检查”。";
    } else {
      statusLine.className = "muted small";
      statusLine.textContent =
        "当前版本 " + (s.current || "未知") + "；尚未到检查时间。";
    }
    // A marker left by the previous apply is only reported when the version it
    // installed is not the one now running.
    if (s.apply && s.apply.confirmed === false) {
      statusLine.className = "error small";
      statusLine.textContent =
        "上次更新没有生效：期望运行 " +
        s.apply.expected +
        "，当前是 " +
        s.apply.running +
        "。可以回到之前的版本。";
      rollback.classList.remove("hidden");
    } else if (s.apply && s.apply.confirmed === true) {
      statusLine.className = "muted small";
      statusLine.textContent = "已更新到 " + s.apply.version + "。";
    }
    if (s.staged) {
      paintDownload({
        status: "ready",
        version: s.staged.version,
        total: s.staged.size,
      });
    }
  }

  schedule.onchange = async () => {
    try {
      await api("update-schedule-save", { schedule: schedule.value });
      notify("更新检查频率已保存。");
    } catch (e) {
      notify("保存检查频率失败：" + e.message, true);
    }
  };
  // Do not hold up the settings page: the first paint wins, the answer follows.
  api("update-status")
    .then(paint)
    .catch((e) => {
      statusLine.className = "error small";
      statusLine.textContent = "读取更新状态失败：" + e.message;
    });
  api("update-download-status")
    .then((state) => {
      if (state?.status === "downloading") {
        paintDownload(state);
        pollDownload();
      }
    })
    .catch(() => {});
  return card;
}

Object.assign(FoolshipDesktop.render, { renderAccount, renderSettings });
Object.assign(FoolshipDesktop.components, { aiInstallSetup });
