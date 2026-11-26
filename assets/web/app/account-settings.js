"use strict";

function manageHeading(title) {
  const heading = el("header", "top manage-heading");
  heading.append(el("h1", "", title));
  return heading;
}

function renderAccount() {
  S.epoch++;
  nav("account");
  const c = $("content");
  c.replaceChildren(manageHeading(t("账户", "Account")));
  const list = el("div", "manage-page manage-account");
  const account = el("section", "panel manage-panel");
  account.append(el("h2", "", t("关联设备", "Linked devices")));
  const devices = el("div", "manage-list");
  S.dash.devices
    .filter((d) => !d.server || d.server === S.server)
    .forEach((d) => {
      const row = el("div", "manage-row");
      const info = el("div", "manage-row-info");
      const details = el("div", "manage-meta");
      details.append(
        el(
          "span",
          "manage-status " + (d.online ? "is-good" : ""),
          d.online ? t("在线", "Online") : t("离线", "Offline"),
        ),
      );
      if (d.local) details.append(el("span", "", t("当前设备", "This device")));
      if (d.is_root)
        details.append(el("span", "", t("根设备", "Primary device")));
      info.append(el("strong", "", d.name), details);
      const actions = el("div", "manage-actions");
      if (!d.is_root)
        actions.append(
          button(t("重命名", "Rename"), () =>
            modal(t("重命名设备", "Rename device"), (b, a) => {
              const input = field(b, t("显示名称", "Display name"), d.name);
              a.append(
                button(t("取消", "Cancel"), closeModal),
                button(
                  t("保存", "Save"),
                  async () => {
                    const value = input.value.trim();
                    if (!value)
                      throw new Error(t("名称不能为空", "Enter a device name"));
                    await api("device-rename", {
                      id: d.id || d.node,
                      name: value,
                      server: d.server || S.server,
                    });
                    closeModal();
                    notify(t("设备名称已更新", "Device name updated"));
                    await refresh(true);
                  },
                  "primary",
                ),
              );
            }),
          ),
        );
      if (!d.is_root && !d.local)
        actions.append(
          button(t("移除", "Remove"), () =>
            modal(t("移除设备", "Remove device"), (b, a) => {
              note(
                b,
                t(
                  `将撤销 ${d.name} 的账户接入权限。`,
                  `${d.name} will lose access to this account.`,
                ),
              );
              a.append(
                button(t("取消", "Cancel"), closeModal),
                button(
                  t("确认移除", "Remove device"),
                  async () => {
                    await api("device-revoke", {
                      id: d.id || d.node,
                      server: d.server || S.server,
                    });
                    closeModal();
                    await refresh(true);
                  },
                  "danger",
                ),
              );
            }),
          ),
        );
      row.append(info);
      if (actions.childElementCount) row.append(actions);
      devices.append(row);
    });
  if (!devices.childElementCount)
    devices.append(
      el("p", "manage-empty", t("暂无关联设备。", "No linked devices.")),
    );
  account.append(devices);
  const linkActions = el("div", "manage-actions manage-footer");
  linkActions.append(
    button(
      t("关联设备", "Link device"),
      () =>
        modal(t("关联新电脑", "Link a new computer"), (b, a) => {
          const code = field(
            b,
            t("新电脑显示的关联码", "Code shown on the new computer"),
          );
          a.append(
            button(t("取消", "Cancel"), closeModal),
            button(
              t("核对请求", "Review request"),
              async () => {
                await api("device-inspect", { code: code.value.trim() });
                closeModal();
                await refresh(true);
              },
              "primary",
            ),
          );
        }),
      "primary",
    ),
  );
  linkActions.append(
    button(
      t("在另一台电脑上安装", "Install on another computer"),
      aiInstallSetup,
    ),
  );
  account.append(linkActions);
  const recovery = el("section", "panel manage-panel");
  recovery.append(el("h2", "", t("账户恢复", "Account recovery")));
  const recoveryRow = el("div", "manage-row");
  recoveryRow.append(
    el(
      "p",
      "manage-meta",
      t(
        "用于在新设备上恢复账户身份。",
        "Restore your account on a new device.",
      ),
    ),
    button(t("创建离线恢复包", "Create offline recovery file"), () =>
      modal(t("创建离线恢复包", "Create offline recovery file"), (b, a) => {
        const password = field(
          b,
          t("恢复包密码", "Recovery file password"),
          "",
          null,
          "password",
        );
        const confirm = field(
          b,
          t("再次输入", "Confirm password"),
          "",
          null,
          "password",
        );
        a.append(
          button(t("取消", "Cancel"), closeModal),
          button(
            t("选择保存位置", "Choose where to save"),
            async () => {
              if (!password.value || password.value !== confirm.value)
                throw new Error(
                  t(
                    "两次输入的恢复包密码不一致",
                    "The recovery passwords do not match",
                  ),
                );
              const result = await api("recovery-export", {
                recovery_password: password.value,
              });
              password.value = "";
              confirm.value = "";
              if (result.saved) {
                closeModal();
                notify(
                  t(
                    `恢复包已保存：${result.name}`,
                    `Recovery file saved: ${result.name}`,
                  ),
                );
              }
            },
            "primary",
          ),
        );
      }),
    ),
  );
  recovery.append(recoveryRow);
  const pending = S.dash.pending_devices || [];
  if (pending.length) {
    const requests = el("section", "panel manage-panel manage-attention");
    requests.append(
      el("h2", "", t("待确认的关联请求", "Pending device links")),
    );
    const rows = el("div", "manage-list");
    pending.forEach((d) => {
      const r = el("div", "manage-row");
      r.append(
        el("strong", "", d.name),
        button(t("核对并批准", "Review and approve"), () =>
          modal(t("关联设备确认", "Review device link"), (b, a) => {
            note(
              b,
              t(
                "仅批准你自己发起的请求。",
                "Approve only requests you started.",
              ),
            );
            b.append(
              el("p", "", t(`名称：${d.name}`, `Name: ${d.name}`)),
              el(
                "p",
                "",
                t(
                  `设备指纹：${d.public_key_fingerprint}`,
                  `Device fingerprint: ${d.public_key_fingerprint}`,
                ),
              ),
              el(
                "p",
                "",
                t(`有效期：${date(d.expires)}`, `Expires: ${date(d.expires)}`),
              ),
            );
            a.append(
              button(t("取消", "Cancel"), closeModal),
              button(
                t("批准关联", "Approve link"),
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
      rows.append(r);
    });
    requests.append(rows);
    list.append(requests);
  }
  renderApprovals(list);
  const actions = el("section", "panel manage-panel");
  actions.append(el("h2", "", t("本机登录", "Signed in on this device")));
  const current = el("div", "manage-row");
  const identity = el("div", "manage-row-info");
  identity.append(el("strong", "", S.dash.user.username));
  const local = $("localName").textContent;
  if (local) identity.append(el("span", "manage-meta", local));
  const row = el("div", "manage-actions");
  row.append(
    button(t("退出账户", "Sign out"), () => $("logout").click()),
    button(t("完全退出应用", "Quit application"), () => $("quitApp").click()),
  );
  current.append(identity, row);
  actions.append(current);
  list.append(account, recovery, actions);
  c.append(list);
}

function appearanceSettingsCard() {
  const card = el("section", "panel manage-panel settings-wide");
  const row = el("div", "manage-preference");
  const choice = field(
    row,
    t("界面语言", "Display language"),
    interfaceLanguagePreference(),
    languagePreferenceOptions(),
  );
  choice.id = "interfaceLanguage";
  row.querySelector("label").htmlFor = choice.id;
  choice.onchange = () =>
    run(async () => {
      try {
        await setInterfaceLanguage(choice.value);
        if (S.dash) updateProfile();
        if (S.page === "settings") {
          await renderSettings();
          $(choice.id)?.focus({ preventScroll: true });
        }
      } finally {
        if (choice.isConnected) choice.value = interfaceLanguagePreference();
      }
    }, choice);
  card.append(row);
  return card;
}

async function renderSettings() {
  nav("settings");
  const settings = await api("settings-get");
  if (S.page !== "settings") return;
  const c = $("content");
  c.replaceChildren(manageHeading(t("设置", "Settings")));
  const layout = el("div", "manage-page settings-layout");
  const index = el("nav", "settings-index");
  index.setAttribute("aria-label", t("设置分类", "Settings categories"));
  const list = el("div", "settings-sections");
  function group(title, ...cards) {
    const section = el("section", "settings-group");
    const heading = el("h2", "settings-group-title", title);
    const cardList = el("div", "settings-card-grid");
    heading.tabIndex = -1;
    const id = "settings-group-" + list.childElementCount;
    heading.id = id;
    section.setAttribute("aria-labelledby", id);
    cardList.append(...cards);
    section.append(heading, cardList);
    index.append(
      button(title, () => {
        section.scrollIntoView({ block: "start" });
        heading.focus({ preventScroll: true });
      }),
    );
    list.append(section);
  }
  group(t("外观", "Appearance"), appearanceSettingsCard());
  const team = teamServiceCard(settings.team_service);
  const machine = el("section", "panel manage-panel settings-wide");
  machine.append(el("h3", "", t("本机接收端", "Local receiver")));
  const receivers = Array.isArray(settings.receivers) ? settings.receivers : [];
  const services = settings.services || [
    { server: settings.server, primary: true, attached: true },
  ];
  const endpoints = el("div", "manage-list");
  services.forEach((service) => {
    const receiver = receivers.find((item) => item.server === service.server);
    const state = !service.attached
      ? t("未接入这台机器", "Device not attached")
      : receiver?.running
        ? receiver.online
          ? t("运行中 · 已连接", "Running · Connected")
          : t("运行中 · 未连接", "Running · Disconnected")
        : t("已停止", "Stopped");
    const row = el("div", "manage-row");
    const info = el("div", "manage-row-info");
    const details = el("div", "manage-meta");
    details.append(
      el(
        "span",
        "manage-status " +
          (receiver?.running && receiver.online ? "is-good" : ""),
        state,
      ),
    );
    if (service.primary)
      details.append(el("span", "", t("主服务", "Primary service")));
    info.append(el("strong", "", serviceName(service.server)), details);
    row.append(info);
    if (!service.attached)
      row.append(
        button(t("接入这台机器", "Attach this device"), () =>
          modal(t("接入这台机器", "Attach this device"), (b, a) => {
            const name = serviceName(service.server);
            note(
              b,
              t(
                `接入后可以在 ${name} 的项目里选择本机执行 Agent。接入只影响这台机器。`,
                `Projects on ${name} can use this device to run agents. This changes only the current device.`,
              ),
            );
            a.append(
              button(t("取消", "Cancel"), closeModal),
              button(
                t("确认接入", "Attach device"),
                async () => {
                  await api("receiver-attach", { server: service.server });
                  closeModal();
                  notify(
                    t(
                      `本机已接入 ${name}`,
                      `This device is attached to ${name}`,
                    ),
                  );
                  await refresh(true);
                },
                "primary",
              ),
            );
          }),
        ),
      );
    endpoints.append(row);
  });
  const running = receivers.filter((item) => item.running);
  const controls = el("div", "manage-actions manage-footer");
  const startReceiver = button(
    t("启动接收", "Start receiver"),
    async () => {
      await api("receiver-start");
      notify(t("本机接收服务已启动", "Local receiver started"));
      await refresh(true);
    },
    "primary",
  );
  const stopReceiver = button(t("停止接收", "Stop receiver"), async () => {
    await api("receiver-stop");
    notify(
      t(
        "接收服务已停止；已启动的作业独立运行。",
        "Receiver stopped. Active jobs continue independently.",
      ),
    );
    await refresh(true);
  });
  controls.append(
    startReceiver,
    stopReceiver,
    button(t("完全退出", "Quit application"), requestAppClose),
  );
  machine.append(endpoints, controls);
  const attached = services.filter((service) => service.attached);
  startReceiver.disabled =
    attached.length > 0 && running.length === attached.length;
  stopReceiver.disabled = running.length === 0;
  group(t("服务与接收", "Services and receivers"), team, machine);
  group(
    t("模型与运行", "Models and execution"),
    executorCard(settings.executor),
    codexCliCard("codex"),
    codexCliCard("dsh"),
    proxySettingsCard(settings.proxy),
    idleSettingsCard(settings.warm_idle_seconds),
  );
  group(
    t("协作接入", "Integrations"),
    codexMcpCard(settings.codex_mcp),
    dshMcpCard(settings.dsh_mcp),
  );
  const diagnostics = el("section", "panel manage-panel");
  diagnostics.append(
    el("h3", "", t("安全诊断", "Redacted diagnostics")),
    el(
      "p",
      "muted small",
      t(
        "生成可分享的诊断信息，隐去身份、项目内容和本机路径。",
        "Generate a shareable report with account information, project content, and local paths removed.",
      ),
    ),
    button(t("查看安全诊断", "View diagnostics"), () =>
      run(async () => {
        const snapshot = await api("diagnostics-get");
        modal(t("安全诊断", "Redacted diagnostics"), (body, actions) => {
          const output = document.createElement("textarea");
          output.readOnly = true;
          output.rows = 18;
          output.value = JSON.stringify(snapshot, null, 2);
          body.append(output);
          actions.append(
            button(t("关闭", "Close"), closeModal),
            button(
              t("复制", "Copy"),
              async () => {
                await copyText(output.value, output);
                notify(t("安全诊断已复制", "Diagnostics copied"));
              },
              "primary",
            ),
          );
        });
      }),
    ),
  );
  const advanced = el("details", "panel manage-panel");
  advanced.append(
    el("summary", "", t("连接与数据位置", "Connection and storage")),
  );
  const server = field(
    advanced,
    t("协作服务地址", "Coordination service URL"),
    settings.server,
  );
  server.readOnly = true;
  if (settings.data_root) {
    const dataPath = field(
      advanced,
      t("账户数据目录", "Account data folder"),
      settings.data_root,
    );
    dataPath.readOnly = true;
  }
  group(t("维护", "Maintenance"), updateCard(), diagnostics, advanced);
  layout.append(index, list);
  c.append(layout);
  renderApprovals(list);
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
    [
      "linux",
      t("Linux 服务器（无界面接收端）", "Linux server (headless receiver)"),
    ],
    ["windows", t("Windows 电脑", "Windows PC")],
    ["macos", t("macOS 电脑（Apple Silicon）", "Mac (Apple silicon)")],
  ];
  modal(
    t("用 AI 在另一台电脑上安装", "Install on another computer with AI"),
    (body, actions) => {
      if (!services.length) {
        note(
          body,
          t(
            "当前账户还没有可供另一台机器访问的 HTTPS 团队入口。请先在设置 → 协作服务中创建团队入口。",
            "Set up an HTTPS team URL under Settings → Coordination service before connecting another device.",
          ),
        );
        actions.append(button(t("关闭", "Close"), closeModal));
        return;
      }
      const platform = field(
        body,
        t("目标机器", "Target device"),
        "linux",
        platforms,
      );
      const service = field(
        body,
        t("接入的协作服务", "Coordination service"),
        services[0].server,
        services.map((item) => [item.server, serviceName(item.server)]),
      );
      const output = document.createElement("textarea");
      output.readOnly = true;
      output.rows = 18;
      output.placeholder = t("正在生成提示词…", "Generating setup prompt…");
      body.append(output);
      const copy = button(
        t("复制给那台机器的 AI", "Copy setup prompt"),
        async () => {
          if (!output.value)
            throw new Error(
              t("提示词尚未生成", "The setup prompt is not ready yet"),
            );
          await copyText(output.value, output);
          notify(t("安装提示词已复制", "Setup prompt copied"));
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
      actions.append(button(t("关闭", "Close"), closeModal), copy);
      run(generate);
    },
  );
}

function updateCard() {
  const card = el("section", "panel manage-panel settings-wide");
  card.append(el("h3", "", t("应用更新", "App updates")));
  const scheduleField = el("div", "manage-form-field");
  const schedule = field(
    scheduleField,
    t("检查频率", "Check frequency"),
    "weekly",
    [
      ["daily", t("每天", "Daily")],
      ["weekly", t("每周", "Weekly")],
      ["monthly", t("每月", "Monthly")],
      ["never", t("从不", "Never")],
    ],
  );
  schedule.id = "updateSchedule";
  scheduleField.querySelector("label").htmlFor = schedule.id;
  const statusLine = el(
    "p",
    "manage-meta",
    t("正在读取更新状态…", "Checking update status…"),
  );
  const actions = el("div", "manage-actions manage-footer");
  const check = button(t("立即检查", "Check now"), async () => {
    check.disabled = true;
    try {
      paint(await api("update-status", { force: true }));
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = t("检查失败：", "Could not check: ") + e.message;
    } finally {
      check.disabled = false;
    }
  });
  const fetchButton = button(
    t("下载更新", "Download update"),
    async () => {
      fetchButton.disabled = true;
      try {
        paintDownload(await api("update-download"));
        pollDownload();
      } catch (e) {
        notify(
          t("开始下载失败：", "Could not start download: ") + e.message,
          true,
        );
        fetchButton.disabled = false;
      }
    },
    "primary",
  );
  const install = button(
    t("安装并重启", "Install and restart"),
    async () => {
      await begin("update-apply", false);
    },
    "primary",
  );
  const installAnyway = button(
    t("仍然安装并重启", "Install and restart anyway"),
    async () => {
      await begin("update-apply", true);
    },
  );
  const rollback = button(t("回到上一版本", "Roll back"), async () => {
    await begin("update-rollback", true);
  });
  const discard = button(t("删除已下载的更新", "Delete download"), async () => {
    await api("update-discard");
    base = { ...base, staged: null };
    paintDownload({ status: "idle" });
    notify(t("已删除下载的安装包。", "Downloaded update deleted."));
  });
  const open = button(t("打开下载页", "Open release page"), async () => {
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
  card.append(statusLine, scheduleField, actions);

  async function begin(action, force) {
    for (const item of [install, installAnyway, rollback]) item.disabled = true;
    try {
      const result = await api(action, { force });
      notify(
        action === "update-rollback"
          ? t(
              `正在回退到 ${result.version}，应用会自动重启。`,
              `Rolling back to ${result.version}. The app will restart.`,
            )
          : t(
              `正在安装 ${result.version}，应用会自动重启。`,
              `Installing ${result.version}. The app will restart.`,
            ),
      );
      await api("app-exit");
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = e.message;
      installAnyway.classList.toggle(
        "hidden",
        !String(e.originalMessage || e.message).includes("会中断"),
      );
    } finally {
      install.disabled = false;
      rollback.disabled = false;
    }
  }

  let latest = "";
  let poller = null;
  let base = {};
  let live = null;

  function megabytes(bytes) {
    return (Number(bytes || 0) / 1048576).toFixed(1) + " MB";
  }

  function hideActions() {
    for (const item of [
      fetchButton,
      install,
      installAnyway,
      rollback,
      discard,
      open,
    ])
      item.classList.add("hidden");
  }

  // One situation, one line, one primary action. Three independent facts feed
  // this card — the release check, the downloaded artifact, the last apply — and
  // they are ranked instead of painted over each other:
  //
  //   last apply failed   -> why it failed + rollback
  //   downloading         -> progress, nothing else
  //   download failed     -> the error + retry
  //   downloaded (newer)  -> install + delete
  //   apply just confirmed-> "updated to X", nothing else
  //   release is newer    -> "new version X" + download + release page
  //   up to date          -> current version, no action
  //   offline / disabled  -> the reason, check is still available
  //   not due yet         -> current version and when it was last checked
  function render() {
    const s = base || {};
    const d = live || {};
    const staged = s.staged && s.staged.version ? s.staged : null;
    const downloading = d.status === "downloading" || d.status === "verifying";
    const ready = !downloading && (d.status === "ready" || Boolean(staged));
    const version = (staged && staged.version) || d.version || s.latest || "";
    const size = (staged && staged.size) || d.total;
    hideActions();
    check.disabled = downloading;
    statusLine.className = "muted small";

    if (s.apply && s.apply.confirmed === false) {
      statusLine.className = "error small";
      statusLine.textContent = t(
        `上次更新没有生效：期望运行 ${s.apply.expected}，当前是 ${s.apply.running}。可以回到之前的版本。`,
        `The update did not take effect. Expected ${s.apply.expected}, running ${s.apply.running}. You can roll back.`,
      );
      if (s.apply.expected_build) {
        const expected = s.apply.expected_build.slice(0, 12);
        const running =
          s.apply.running_build?.slice(0, 12) || t("无法验证", "unverified");
        statusLine.textContent += t(
          ` 期望构建 ${expected}，实际构建 ${running}。`,
          ` Expected build ${expected}, running ${running}.`,
        );
      }
      rollback.classList.remove("hidden");
      if (s.apply_log?.path) {
        const line = (s.apply_log.lines || []).at(-1) || t("尚未写入", "empty");
        statusLine.textContent += t(
          ` 更新助手日志：${s.apply_log.path}（${line}）`,
          ` Updater log: ${s.apply_log.path} (${line})`,
        );
      }
      return;
    }
    if (downloading) {
      const total = d.total
        ? megabytes(d.total)
        : t("未知大小", "Unknown size");
      const percent = d.total
        ? Math.min(100, Math.round((Number(d.received || 0) / d.total) * 100))
        : null;
      statusLine.className = "small";
      const amount = `${megabytes(d.received)} / ${total}`;
      const progress =
        percent === null
          ? amount
          : t(`${amount}（${percent}%）`, `${amount} (${percent}%)`);
      statusLine.textContent = t(
        `正在下载 ${progress}；安装仍需你确认。`,
        `Downloading ${progress}. Installation requires confirmation.`,
      );
      return;
    }
    if (d.status === "failed") {
      statusLine.className = "error small";
      statusLine.textContent =
        t("下载失败：", "Download failed: ") +
        (localizeDesktopText(d.error) || t("未知错误", "Unknown error"));
      fetchButton.classList.remove("hidden");
      return;
    }
    if (ready) {
      statusLine.className = "small";
      const amount = megabytes(size);
      statusLine.textContent = t(
        `已下载 v${version}（${amount}），SHA-256 已校验。安装会先退出应用，替换完成后自动重启。`,
        `Downloaded v${version} (${amount}). SHA-256 verified. The app will restart after installation.`,
      );
      // The download can lag behind the release page; say so instead of letting
      // the older artifact look like the newest one.
      if (
        s.status === "update" &&
        s.latest &&
        String(s.latest).replace(/^v/, "") !== String(version)
      ) {
        statusLine.textContent += t(
          ` 发布页已有 ${s.latest}：先删除已下载的包，再重新下载。`,
          ` Release ${s.latest} is available. Delete this download to get the newer release.`,
        );
      }
      install.classList.remove("hidden");
      discard.classList.remove("hidden");
      return;
    }
    if (s.apply && s.apply.confirmed === true) {
      statusLine.textContent = t(
        `已更新到 v${s.apply.version}。`,
        `Updated to v${s.apply.version}.`,
      );
      return;
    }
    const current = s.current || t("未知", "unknown");
    if (s.status === "update") {
      statusLine.className = "small";
      statusLine.textContent = t(
        `有新版本 ${s.latest || ""}（当前 ${current}）。`,
        `Update ${s.latest || ""} is available (current: ${current}).`,
      );
      fetchButton.classList.remove("hidden");
      open.classList.remove("hidden");
      return;
    }
    if (s.status === "current") {
      statusLine.textContent = t(
        `已是最新版本 ${current}。`,
        `You are up to date (${current}).`,
      );
      return;
    }
    if (s.status === "offline") {
      statusLine.textContent = t(
        "暂时连不上发布页，稍后会自动重试。",
        "The release page is unavailable. The app will retry later.",
      );
      return;
    }
    if (s.status === "disabled") {
      statusLine.textContent = t(
        "已关闭自动检查；仍可点“立即检查”。",
        "Automatic checks are off. You can still check for updates now.",
      );
      return;
    }
    statusLine.textContent = t(
      `当前版本 ${current}；尚未到检查时间。`,
      `Current version: ${current}. The next check is scheduled.`,
    );
  }

  function paint(state) {
    base = state || {};
    live = null;
    latest = base.latest || latest;
    if (base.schedule) schedule.value = base.schedule;
    render();
  }

  function paintDownload(state) {
    live = state || {};
    if (
      live.status === "ready" ||
      live.status === "failed" ||
      live.status === "idle"
    ) {
      if (poller) {
        clearInterval(poller);
        poller = null;
      }
    }
    render();
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

  schedule.onchange = async () => {
    try {
      await api("update-schedule-save", { schedule: schedule.value });
      notify(t("更新检查频率已保存。", "Update check frequency saved."));
    } catch (e) {
      notify(
        t("保存检查频率失败：", "Could not save frequency: ") + e.message,
        true,
      );
    }
  };
  // Do not hold up the settings page: the first paint wins, the answer follows.
  api("update-status")
    .then(paint)
    .catch((e) => {
      statusLine.className = "error small";
      statusLine.textContent =
        t("读取更新状态失败：", "Could not load update status: ") + e.message;
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
