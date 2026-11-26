"use strict";
function renderApprovals(parent) {
  const approvals = S.dash.pending_approvals || [];
  if (!approvals.length) return;
  const panel = el("section", "panel manage-panel manage-attention");
  panel.append(el("h2", "", t("待批准的执行", "Pending execution approvals")));
  const list = el("div", "manage-list");
  approvals.forEach((j) => {
    const row = el("div", "manage-row"),
      info = el("div", "manage-row-info");
    info.append(el("strong", "", j.title || j.template || j.job_id));
    if (j.cwd) info.append(el("p", "manage-path", j.cwd));
    row.append(
      info,
      button(t("审阅", "Review"), () =>
        modal(t("批准本机后台作业", "Approve local background job"), (b, a) => {
          if (j.template)
            b.append(
              el(
                "p",
                "",
                t(
                  `执行模板：${j.template}`,
                  `Execution template: ${j.template}`,
                ),
              ),
            );
          if (j.cwd)
            b.append(
              el(
                "p",
                "manage-path",
                t(`工作目录：${j.cwd}`, `Working directory: ${j.cwd}`),
              ),
            );
          if (j.argv || j.command) {
            b.append(
              el("p", "label", t("命令及参数", "Command and arguments")),
              el(
                "pre",
                "approval-command",
                j.argv ? JSON.stringify(j.argv, null, 2) : j.command,
              ),
            );
          }
          const details = el("details", "approval-details");
          details.append(
            el("summary", "", t("完整任务信息", "Full job details")),
            el("pre", "approval-command", JSON.stringify(j, null, 2)),
          );
          b.append(details);
          a.append(
            button(t("拒绝", "Decline"), async () => {
              await api("approve-execution", {
                job_id: j.job_id,
                approved: false,
              });
              closeModal();
              await refresh(true);
            }),
            button(
              t("批准", "Approve"),
              async () => {
                await api("approve-execution", {
                  job_id: j.job_id,
                  approved: true,
                  expected_template: j.expected_template,
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
    list.append(row);
  });
  panel.append(list);
  parent.append(panel);
}

let teamTimer = null,
  teamGeneration = 0;
function teamServiceMessage(value) {
  if (!value || interfaceLocale() === "zh-CN") return value;
  const messages = {
    "当前账户连接其它机器的服务，请在托管电脑配置团队入口。":
      "Set up the team URL on the computer hosting this service.",
    "当前服务由其它机器托管。": "Another computer hosts this service.",
    "请先在 Tailscale 中登录并连接网络。":
      "Sign in to Tailscale and connect to your network.",
    "尚未获得有效的 Tailscale HTTPS 域名，请检查 MagicDNS。":
      "No valid Tailscale HTTPS domain was found. Check MagicDNS.",
    "入口已配置。复制地址给获准加入 Tailscale 网络的同事。":
      "Share the URL with team members who have access to your Tailscale network.",
    "尚未创建团队入口。": "Team URL not set up.",
    "443 端口有其它 Serve 配置，应用不会覆盖或停止它。":
      "Port 443 is already in use by a Tailscale Serve configuration.",
    "上次配置未完成，请重新创建入口。":
      "The last setup did not finish. Create the team URL again.",
    "正在创建团队入口…": "Creating team URL…",
    "配置程序未启动，请检查应用是否完整。":
      "Setup did not start. Check the app installation.",
    "正在配置 HTTPS 团队入口…": "Configuring the HTTPS team URL…",
    "需要在 Tailscale 页面确认启用 Serve / HTTPS。":
      "Allow Serve and HTTPS in Tailscale to continue.",
    "无法检查 Tailscale 状态": "Could not check Tailscale status",
    团队入口配置失败: "Team URL setup failed",
    "连接成功：当前账户可通过此地址访问协作服务。":
      "This account can access the coordination service at this URL.",
  };
  return Object.hasOwn(messages, value)
    ? messages[value]
    : localizeDesktopText(value);
}
function teamServiceCard(initial) {
  const card = el("section", "panel manage-panel settings-wide");
  card.id = "teamServiceCard";
  let signature = "",
    last = initial || {},
    generation = ++teamGeneration;
  clearTimeout(teamTimer);
  function active() {
    return (
      !quitDialog &&
      generation === teamGeneration &&
      S.page === "settings" &&
      $("content").contains(card)
    );
  }
  function schedule(status) {
    clearTimeout(teamTimer);
    if (["starting", "needs_authorization"].includes(status.state))
      teamTimer = setTimeout(() => run(update), 1500);
  }
  async function update() {
    if (!active()) return;
    try {
      const status = await api("team-service-status");
      if (!active()) return;
      paint(status);
    } finally {
      if (active()) schedule(last);
    }
  }
  async function action(name) {
    const status = await api(name);
    if (!active()) return;
    if (status?.state) paint(status);
    else await update();
  }
  function paint(status) {
    last = status || {};
    const next = JSON.stringify(last);
    if (next === signature) {
      schedule(last);
      return;
    }
    signature = next;
    card.replaceChildren(el("h3", "", t("协作服务", "Coordination service")));
    const labels = {
      ready: t("团队入口已就绪", "Team URL ready"),
      disabled: t("尚未创建团队入口", "Team URL not set up"),
      needs_login: t("请先登录 Tailscale", "Sign in to Tailscale"),
      missing: t("本机尚未安装 Tailscale", "Tailscale is not installed"),
      conflict: t(
        "已有连接配置，需要先核对",
        "Existing configuration needs review",
      ),
      starting: t("正在创建团队入口…", "Creating team URL…"),
      needs_authorization: t(
        "等待 Tailscale 确认授权",
        "Awaiting Tailscale approval",
      ),
      error: t("团队入口暂不可用", "Team URL unavailable"),
      remote: t(
        "当前账户使用远端协作服务",
        "This account uses a remote coordination service",
      ),
    };
    const statusLabel =
      labels[last.state] || t("正在检查团队入口", "Checking team URL…");
    const statusLine = el("div", "manage-status-line");
    statusLine.append(
      el(
        "span",
        "manage-status " +
          ({
            ready: "is-good",
            error: "is-error",
            conflict: "is-attention",
            needs_authorization: "is-attention",
          }[last.state] || ""),
        statusLabel,
      ),
    );
    card.append(statusLine);
    const message = teamServiceMessage(last.message);
    if (
      message &&
      message.replace(/[.。…]+$/, "") !== statusLabel.replace(/[.。…]+$/, "")
    )
      card.append(el("p", "manage-meta", message));
    if (last.url) {
      const address = field(
        card,
        t("团队服务地址", "Team service URL"),
        last.url,
      );
      address.readOnly = true;
      address.setAttribute("aria-label", t("团队服务地址", "Team service URL"));
      note(
        card,
        last.state === "remote" && !last.url.startsWith("https://")
          ? t(
              "这是当前电脑的本机转发地址，不能直接交给其它电脑。请向服务托管者获取 HTTPS 团队地址。",
              "This address forwards traffic on this device only. Ask the service host for the team's HTTPS URL.",
            )
          : t(
              "加入项目需要这个地址、项目邀请码及 Tailscale 访问权限。",
              "You need this URL, a project invite code, and Tailscale access to join a project.",
            ),
      );
    } else if (last.state === "conflict")
      note(
        card,
        t(
          "这台机器上已有同名配置，应用没有改动它。先确认原来那个配置不再需要，再继续。",
          "A configuration with this name already exists. Confirm it is no longer needed before continuing.",
        ),
      );
    const controls = el("div", "manage-actions manage-footer");
    const create = button(
      t("创建团队入口", "Create team URL"),
      () => action("team-service-create"),
      "primary",
    );
    create.disabled =
      !last.can_manage || !["disabled", "error"].includes(last.state);
    if (!create.disabled) controls.append(create);
    const copy = button(t("复制地址", "Copy URL"), async () => {
      await api("team-service-copy");
      notify(t("团队地址已复制。", "Team URL copied."));
    });
    copy.disabled =
      !last.url ||
      !["ready", "remote"].includes(last.state) ||
      (last.state === "remote" && !last.url.startsWith("https://"));
    if (!copy.disabled) controls.append(copy);
    const test = button(t("检测连接", "Test connection"), async () => {
      const result = await api("team-service-test");
      notify(
        teamServiceMessage(result.message) ||
          (result.ok
            ? t("连接检测通过", "Connection successful")
            : t("连接检测未通过", "Connection failed")),
        result.ok === false,
      );
      await update();
    });
    test.disabled = !last.url || !["ready", "remote"].includes(last.state);
    if (!test.disabled) controls.append(test);
    const backupControls = el("div", "manage-row manage-backups");
    if (last.can_backup) {
      const backupInfo = el("div", "manage-row-info");
      backupInfo.append(
        el("strong", "", t("项目数据备份", "Project data backup")),
        el(
          "span",
          "manage-meta",
          t(
            "包含消息、成员和任务记录，请妥善保管。",
            "The backup contains messages, members, and tasks. Store it securely.",
          ),
        ),
      );
      const backupActions = el("div", "manage-actions");
      backupActions.append(
        button(t("备份项目数据", "Back up project data"), async () => {
          const result = await api("team-service-backup");
          if (result.saved)
            notify(
              t(`备份已保存：${result.name}`, `Backup saved: ${result.name}`),
            );
        }),
      );
      backupActions.append(
        button(t("从备份恢复", "Restore from backup"), async () => {
          const selected = await api("team-service-restore-select");
          if (!selected.selected) return;
          modal(
            t("确认恢复协作服务", "Restore coordination service"),
            (body, actions) => {
              body.append(
                el(
                  "p",
                  "",
                  t(
                    `备份文件：${selected.name}`,
                    `Backup file: ${selected.name}`,
                  ),
                ),
                el(
                  "p",
                  "muted",
                  t(
                    `其中包含 ${selected.projects} 个项目、${selected.users} 个账户。恢复会用备份中的项目、成员、消息与任务替换当前协作服务数据。`,
                    `Projects: ${selected.projects}. Accounts: ${selected.users}. Restoring replaces this service's projects, members, messages, and tasks with the backup.`,
                  ),
                ),
              );
              note(
                body,
                t(
                  "恢复前会先备份当前数据；各机器的项目目录和 Agent 设置不受影响。恢复时其他成员会短暂断开。",
                  "Current data is backed up before restoring. Local project folders and agent settings are kept. Other members will disconnect briefly.",
                ),
              );
              actions.append(
                button(t("取消", "Cancel"), closeModal),
                button(
                  t("确认恢复", "Restore backup"),
                  async () => {
                    closeModal();
                    const result = await api("team-service-restore-apply", {
                      token: selected.token,
                    });
                    notify(
                      result.restored
                        ? t(
                            "恢复完成，协作服务已重新启动。",
                            "Restore complete. The coordination service has restarted.",
                          )
                        : t("恢复未完成。", "Restore did not complete."),
                      !result.restored,
                    );
                    await update();
                  },
                  "danger",
                ),
              );
            },
          );
        }),
      );
      backupControls.append(backupInfo, backupActions);
    }
    if (last.state === "needs_authorization") {
      const authorize = button(
        t("打开 Tailscale 授权页面", "Open Tailscale authorization"),
        () => action("team-service-authorize"),
        "primary",
      );
      authorize.disabled = !last.can_manage || !last.authorization_url;
      controls.append(authorize);
      note(
        card,
        t(
          "这一步要在 Tailscale 页面点确认，应用不会替你批准。确认后会自动检查状态。",
          "Confirm the request in Tailscale. The app will then check for approval automatically.",
        ),
      );
    }
    const stop = button(t("停止团队入口", "Stop team URL"), () =>
      modal(t("停止团队入口", "Stop team URL"), (b, a) => {
        note(
          b,
          t(
            "停止后同事不能再通过这个入口连接。账户、项目和任务都保留。",
            "Team members will lose access through this URL. Accounts, projects, and tasks are kept.",
          ),
        );
        a.append(
          button(t("取消", "Cancel"), closeModal),
          button(
            t("确认停止", "Confirm stop"),
            async () => {
              closeModal();
              await action("team-service-stop");
            },
            "primary",
          ),
        );
      }),
    );
    stop.disabled = !last.can_manage || last.state !== "ready";
    if (!stop.disabled) controls.append(stop);
    controls.append(button(t("刷新状态", "Refresh status"), update));
    card.append(controls);
    if (last.can_backup) card.append(backupControls);
    schedule(last);
  }
  // Paint after attachment; the independent poll updates only this card.
  card.refresh = update;
  card.paint = paint;
  return card;
}

Object.assign(FoolshipDesktop.components, {
  renderApprovals,
  teamServiceCard,
  proxySettingsCard,
  idleSettingsCard,
  executorCard,
  codexCliCard,
  codexMcpCard,
  dshMcpCard,
});
function executorCard(current) {
  const card = el("section", "panel manage-panel settings-wide");
  card.append(el("h3", "", t("本机可用模型", "Models on this device")));

  const models = Array.isArray(current?.models)
    ? current.models.map((item) => ({ ...item }))
    : [];
  const rows = el("div", "model-table");
  let timer = null;
  const save = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const cleaned = models.filter((item) => item.name && item.name.trim());
      if (!cleaned.length) {
        notify(t("至少保留一个模型", "Keep at least one model"), true);
        return;
      }
      try {
        const result = await api("executor-save", { models: cleaned });
        const status = result?.status;
        if (status?.found) notify(t("模型表已保存", "Model list saved"));
        else
          notify(
            t(
              "已保存，但没有检测到可运行的 CLI：",
              "Saved, but no usable CLI was found: ",
            ) +
              (executorStatusMessage(status?.error) ||
                t("请检查安装", "Check the installation")),
            true,
          );
        await refresh();
      } catch (error) {
        notify(t("保存失败：", "Could not save: ") + error.message, true);
      }
    }, 600);
  };
  const draw = () => {
    rows.replaceChildren();
    models.forEach((item, index) => {
      const row = el("div", "model-row");
      const name = el("input");
      name.type = "text";
      name.value = item.name || "";
      name.placeholder = t(
        "模型名，例如 deepseek-flash",
        "Model name, e.g. deepseek-flash",
      );
      name.setAttribute(
        "aria-label",
        t(`模型 ${index + 1} 的名称`, `Model ${index + 1} name`),
      );
      name.oninput = () => {
        models[index].name = name.value.trim();
      };
      name.onblur = save;
      const kind = el("select");
      kind.setAttribute(
        "aria-label",
        t(`模型 ${index + 1} 的执行器`, `Model ${index + 1} executor`),
      );
      [
        ["codex", "Codex CLI"],
        ["dsh", "DeepSeek Harness"],
      ].forEach(([value, label]) => {
        const option = el("option", "", label);
        option.value = value;
        kind.append(option);
      });
      kind.value = item.executor === "dsh" ? "dsh" : "codex";
      kind.onchange = () => {
        models[index].executor = kind.value;
        save();
      };
      row.append(
        name,
        kind,
        button(
          t("删除", "Delete"),
          () => {
            models.splice(index, 1);
            draw();
            save();
          },
          "link",
        ),
      );
      rows.append(row);
    });
  };
  draw();
  card.append(rows);
  card.append(
    button(t("添加模型", "Add model"), () => {
      models.push({ name: "", executor: "codex" });
      draw();
      rows.lastElementChild.querySelector("input").focus();
    }),
  );
  return card;
}

function proxySettingsCard(current) {
  const card = el("section", "panel manage-panel");
  card.append(el("h3", "", t("本机代理", "Local proxy")));
  const port = field(
    card,
    t("代理端口（留空时关闭）", "Proxy port (blank to disable)"),
    current?.port ?? "",
    null,
    "number",
  );
  port.min = "1";
  port.max = "65535";
  port.placeholder = t("例如 7897", "e.g. 7897");
  let saved = current?.port ?? "";
  const apply = async () => {
    const raw = port.value.trim();
    if (raw === String(saved ?? "")) return;
    let value = null;
    if (raw) {
      if (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) {
        notify(
          t("端口需在 1 到 65535 之间", "Enter a port between 1 and 65535"),
          true,
        );
        port.focus();
        return;
      }
      value = Number(raw);
    }
    await api("proxy-save", { enabled: value !== null, port: value });
    saved = value ?? "";
    notify(
      value === null
        ? t("已清除本机代理端口", "Local proxy disabled")
        : t(`已保存端口 ${value}`, `Proxy port ${value} saved`),
    );
    await refresh();
  };
  port.onblur = apply;
  port.onkeydown = (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      port.blur();
    }
  };
  return card;
}
function idleSettingsCard(seconds) {
  const card = el("section", "panel manage-panel");
  card.append(el("h3", "", t("Agent 空闲保活", "Agent idle timeout")));
  const choice = field(
    card,
    t("空闲多久后退出", "Close idle sessions after"),
    String(seconds || 300),
    [
      [60, t("1 分钟", "1 minute")],
      [300, t("5 分钟", "5 minutes")],
      [900, t("15 分钟", "15 minutes")],
      [1800, t("30 分钟", "30 minutes")],
      [3600, t("1 小时", "1 hour")],
    ],
  );
  choice.onchange = async () => {
    await api("warm-idle-save", { seconds: Number(choice.value) });
    notify(t("已保存", "Saved"));
    await refresh();
  };
  return card;
}
function executorStatusMessage(value) {
  if (!value || interfaceLocale() === "zh-CN") return value;
  const messages = {
    "凭据由 DeepSeek Harness 管理": "DeepSeek Harness manages credentials",
    "Codex 登录状态检查超时；请检查本机代理":
      "The Codex sign-in check timed out. Check your proxy settings.",
    "无法检查 Codex 登录状态": "Could not check Codex sign-in status",
    "Codex CLI 尚未登录；请使用当前系统用户完成登录":
      "Codex CLI is signed out. Sign in using this system account.",
    "未找到可运行的 Codex CLI；请在本机安装 Codex，或在设置中选择程序":
      "No usable Codex CLI was found. Install Codex or choose the executable in Settings.",
    "未找到可运行的 DeepSeek Harness CLI；请安装 DeepSeek Harness，或在设置中选择程序":
      "No usable DeepSeek Harness CLI was found. Install DeepSeek Harness or choose the executable in Settings.",
  };
  if (Object.hasOwn(messages, value)) return messages[value];
  const missing =
    /^设置的 (Codex|DeepSeek Harness) 路径已失效，请重新选择程序$/.exec(value);
  if (missing)
    return `The ${missing[1]} path is unavailable. Choose the executable again.`;
  const invalid =
    /^所选程序无法运行 (Codex|DeepSeek Harness) CLI，请重新选择$/.exec(value);
  if (invalid)
    return `The selected executable cannot run ${invalid[1]}. Choose a different one.`;
  return localizeDesktopText(value);
}

function codexCliCard(kind) {
  const dsh = kind === "dsh";
  const label = dsh ? "DeepSeek Harness CLI" : "Codex CLI";
  const card = el("section", "panel manage-panel"),
    statusLine = el("p", "manage-meta", t("正在检测…", "Detecting…")),
    fileLocation = el("p", "manage-path"),
    controls = el("div", "manage-actions manage-footer");
  statusLine.setAttribute("role", "status");
  card.append(el("h3", "", label), statusLine, fileLocation);
  const path = field(
    card,
    t(
      "手动指定程序（留空则自动检测）",
      "Executable path (blank to detect automatically)",
    ),
    "",
  );
  path.placeholder = dsh
    ? t("dsh 或 dsh.cmd", "dsh or dsh.cmd")
    : t("codex、codex.exe 或 codex.cmd", "codex, codex.exe, or codex.cmd");
  async function savePath() {
    const value = path.value.trim();
    const r = await api("codex-cli-save", { path: value, kind });
    path.value = value ? r?.path || value : "";
    notify(
      value
        ? t(`${label} 路径已保存。`, `${label} path saved.`)
        : t("已恢复自动检测。", "Automatic detection restored."),
    );
    await load();
  }
  const browse = button(t("浏览…", "Browse…"), async () => {
    const r = await api("codex-cli-choose");
    if (r?.path) {
      path.value = r.path;
      await savePath();
    }
  });
  const detect = button(t("重新检测", "Detect again"), async () => {
    await load(true);
  });
  path.onblur = savePath;
  controls.append(browse, detect);
  card.append(controls);
  async function load(force = false) {
    detect.disabled = true;
    try {
      const r = await api(force ? "codex-cli-redetect" : "codex-cli-status", {
        kind,
      });
      fileLocation.textContent = r?.found ? r.path || "" : "";
      if (r?.found) {
        statusLine.className = r.logged_in
          ? "manage-meta"
          : "manage-meta error";
        statusLine.textContent =
          t("已找到", "Found") +
          (r.version ? " · " + r.version : "") +
          (r.logged_in
            ? " · " +
              (executorStatusMessage(r.credential_note) ||
                t("已登录", "Signed in"))
            : " · " +
              (executorStatusMessage(r.login_error) ||
                t("尚未登录", "Not signed in")));
        if (r.configured && !path.value.trim() && r.path) path.value = r.path;
      } else {
        statusLine.className = "manage-meta error";
        const error = executorStatusMessage(r?.error);
        statusLine.textContent = error
          ? t(`未找到 ${label}：${error}`, `${label} not found: ${error}`)
          : t(
              `未找到 ${label}。请安装后浏览选择 ${label} 程序。`,
              `${label} not found. Install it, then browse for the executable.`,
            );
      }
    } catch (e) {
      fileLocation.textContent = "";
      statusLine.className = "manage-meta error";
      statusLine.textContent =
        t("检测失败：", "Detection failed: ") + e.message;
    } finally {
      detect.disabled = false;
    }
  }
  card.refresh = load;
  load();
  return card;
}
function codexMcpCard(initial) {
  const card = el("section", "panel manage-panel"),
    statusLine = el(
      "p",
      "manage-meta",
      t("正在读取连接状态…", "Loading connection status…"),
    ),
    controls = el("div", "manage-actions manage-footer");
  statusLine.setAttribute("role", "status");
  const action = button(
    t("连接到 Codex", "Connect to Codex"),
    async () => {
      action.disabled = true;
      try {
        const connected = !!card.state?.enabled && !!card.state?.configured;
        const r = await api(
          connected ? "codex-mcp-disable" : "codex-mcp-enable",
        );
        const pending = Number(r?.pending_revocations || 0);
        notify(
          connected
            ? pending
              ? t(
                  `已断开本机连接；${pending} 个离线服务将在恢复后完成撤销。`,
                  `Local connection removed. Revocation remains pending on ${pending} offline ${pending === 1 ? "service" : "services"}.`,
                )
              : t("已断开 Codex 协作连接。", "Codex integration disconnected.")
            : t(
                "已连接 Codex；普通 Codex 对话现在可以使用协作。",
                "Codex is connected. Foolship is now available in regular Codex chats.",
              ),
          pending > 0,
        );
        await load(r?.codex_mcp || r);
      } catch (e) {
        notify(
          t("Codex 协作连接失败：", "Codex connection failed: ") + e.message,
          true,
        );
        action.disabled = false;
      }
    },
    "primary",
  );
  controls.append(action);
  card.append(
    el("h3", "", t("在 Codex 中使用协作", "Codex integration")),
    statusLine,
  );
  card.append(controls);
  function paint(state) {
    state = state || {};
    card.state = state;
    const enabled = !!state.enabled,
      configured = !!state.configured;
    action.textContent =
      enabled && configured
        ? t("断开 Codex", "Disconnect Codex")
        : enabled
          ? t("重试连接", "Retry connection")
          : t("连接到 Codex", "Connect to Codex");
    action.disabled = false;
    action.classList.toggle("primary", !(enabled && configured));
    statusLine.className =
      enabled && !configured ? "manage-meta error" : "manage-meta";
    statusLine.textContent = enabled
      ? configured
        ? t("已连接", "Connected")
        : t("尚未完成配置", "Setup incomplete")
      : t("未连接", "Not connected");
    const old = card.querySelector(".codex-mcp-warnings");
    if (old) old.remove();
    const warnings = Array.isArray(state.warnings)
      ? state.warnings.filter(Boolean)
      : [];
    if (warnings.length) {
      const box = el("div", "note codex-mcp-warnings");
      warnings.forEach((w) =>
        box.append(
          el(
            "div",
            "",
            typeof w === "object"
              ? `${w.server || t("协作服务", "Coordination service")}：${w.error || t("操作失败", "Operation failed")}`
              : String(w),
          ),
        ),
      );
      card.insertBefore(box, controls);
    }
  }
  async function load(state) {
    try {
      paint(state || (await api("codex-mcp-status")));
    } catch (e) {
      statusLine.className = "manage-meta error";
      statusLine.textContent =
        t("读取状态失败：", "Could not load status: ") + e.message;
      action.disabled = false;
    }
  }
  card.refresh = load;
  paint(initial);
  if (!initial) load();
  return card;
}
function dshMcpCard(initial) {
  const card = el("section", "panel manage-panel"),
    statusLine = el(
      "p",
      "manage-meta",
      t("正在读取连接状态…", "Loading connection status…"),
    ),
    controls = el("div", "manage-actions manage-footer");
  statusLine.setAttribute("role", "status");
  const action = button(
    t("连接到 DeepSeek Harness", "Connect to DeepSeek Harness"),
    async () => {
      const connected =
        !!card.state?.enabled && !!card.state?.configured && !card.state?.stale;
      action.disabled = true;
      try {
        const state = await api(
          connected ? "dsh-mcp-disable" : "dsh-mcp-enable",
        );
        notify(
          connected
            ? t(
                "已断开 DeepSeek Harness 协作连接。",
                "DeepSeek Harness integration disconnected.",
              )
            : t(
                "已连接；DeepSeek Harness 的会话现在可以使用协作工具。",
                "DeepSeek Harness is connected. Foolship tools are now available in its sessions.",
              ),
        );
        paint(state);
      } catch (e) {
        notify(
          t(
            "DeepSeek Harness 协作连接失败：",
            "DeepSeek Harness connection failed: ",
          ) + e.message,
          true,
        );
      } finally {
        action.disabled = false;
      }
    },
    "primary",
  );
  controls.append(action);
  card.append(
    el(
      "h3",
      "",
      t("在 DeepSeek Harness 中使用协作", "DeepSeek Harness integration"),
    ),
    statusLine,
    controls,
  );
  function paint(state) {
    if (state) card.state = state;
    const s = card.state || {};
    const connected = !!s.enabled && !!s.configured;
    const stale = !!(s.configured && s.stale);
    action.textContent =
      connected && !stale
        ? t("断开 DeepSeek Harness", "Disconnect DeepSeek Harness")
        : s.configured
          ? t("重新连接", "Reconnect")
          : t("连接到 DeepSeek Harness", "Connect to DeepSeek Harness");
    action.classList.toggle("primary", !connected || stale);
    statusLine.className =
      stale || (s.enabled && !s.configured)
        ? "manage-meta error"
        : "manage-meta";
    statusLine.textContent = stale
      ? t(
          "连接配置已过期，请重新连接。",
          "Connection settings expired. Reconnect to continue.",
        )
      : s.configured
        ? t("已连接", "Connected") + (s.path ? " · " + s.path : "")
        : s.enabled
          ? t(
              "已授权，请重新连接以完成配置。",
              "Authorized. Reconnect to finish setup.",
            )
          : t("未连接", "Not connected");
  }
  async function load(state) {
    if (state) return paint(state);
    try {
      paint(await api("dsh-mcp-status"));
    } catch (e) {
      statusLine.className = "manage-meta error";
      statusLine.textContent =
        t("读取状态失败：", "Could not load status: ") + e.message;
    }
  }
  card.refresh = load;
  paint(initial);
  if (!initial) load();
  return card;
}
