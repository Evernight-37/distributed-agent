"use strict";
function renderApprovals(parent) {
  const approvals = S.dash.pending_approvals || [];
  if (!approvals.length) return;
  const panel = el("section", "panel");
  panel.style.marginTop = "24px";
  panel.append(el("h2", "", "等待你的执行批准"));
  approvals.forEach((j) => {
    const row = el("div", "setting-row"),
      info = el("div");
    info.append(
      el("strong", "", j.title || j.job_id),
      el("p", "muted small", j.command || j.template || "请核对执行内容"),
    );
    row.append(
      info,
      button("审阅", () =>
        modal("批准本机后台作业", (b, a) => {
          b.append(el("pre", "", JSON.stringify(j, null, 2)));
          a.append(
            button("拒绝", async () => {
              await api("approve-execution", {
                job_id: j.job_id,
                approved: false,
              });
              closeModal();
              await refresh(true);
            }),
            button(
              "批准",
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
    panel.append(row);
  });
  parent.append(panel);
}

let teamTimer = null,
  teamGeneration = 0;
function teamServiceCard(initial) {
  const card = el("section", "panel");
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
    card.replaceChildren(el("h2", "", "协作服务"));
    const labels = {
      ready: "团队入口已就绪",
      disabled: "尚未创建团队入口",
      needs_login: "请先登录 Tailscale",
      missing: "本机尚未安装 Tailscale",
      conflict: "已有连接配置，需要先核对",
      starting: "正在创建团队入口…",
      needs_authorization: "等待 Tailscale 确认授权",
      error: "团队入口暂不可用",
      remote: "当前账户使用远端协作服务",
    };
    card.append(
      el(
        "p",
        last.state === "error" || last.state === "conflict" ? "error" : "muted",
        labels[last.state] || "正在检查团队入口",
      ),
    );
    if (last.message) card.append(el("p", "muted small", last.message));
    if (last.url) {
      const address = field(card, "团队服务地址", last.url);
      address.readOnly = true;
      address.setAttribute("aria-label", "团队服务地址");
      note(
        card,
        last.state === "remote" && !last.url.startsWith("https://")
          ? "这是当前电脑的本机转发地址，不能直接交给其它电脑。请向服务托管者获取 HTTPS 团队地址。"
          : "把这个地址和项目邀请代码交给合作伙伴，用于加入项目。访问需要相应的 Tailscale 网络权限；项目邀请仍单独管理。",
      );
    } else
      note(
        card,
        "为本机工作台创建稳定的 HTTPS 团队入口，让已获网络访问权限的设备连接。不会上传或同步项目工作目录。",
      );
    if (last.state === "conflict")
      note(
        card,
        "检测到已有配置，应用不会覆盖。请先核对原有用途，再处理连接配置。",
      );
    if (!last.can_manage)
      note(
        card,
        "这里仅查看当前服务。此入口属于其他机器，本机不能修改它的 Tailscale 设置。",
      );
    const controls = el("div", "row");
    controls.style.flexWrap = "wrap";
    controls.style.justifyContent = "flex-start";
    const create = button(
      "创建团队入口",
      () => action("team-service-create"),
      "primary",
    );
    create.disabled =
      !last.can_manage || !["disabled", "error"].includes(last.state);
    controls.append(create);
    const copy = button("复制地址", async () => {
      await api("team-service-copy");
      notify("团队地址已复制。");
    });
    copy.disabled =
      !last.url ||
      !["ready", "remote"].includes(last.state) ||
      (last.state === "remote" && !last.url.startsWith("https://"));
    controls.append(copy);
    const test = button("检测连接", async () => {
      const result = await api("team-service-test");
      notify(
        result.message || (result.ok ? "连接检测通过" : "连接检测未通过"),
        result.ok === false,
      );
      await update();
    });
    test.disabled = !last.url || !["ready", "remote"].includes(last.state);
    controls.append(test);
    if (last.can_backup) {
      note(
        card,
        "备份文件包含项目消息、成员和任务记录，请像保护账户数据一样妥善保存，不要发送到不可信位置。",
      );
      controls.append(
        button("备份项目数据", async () => {
          const result = await api("team-service-backup");
          if (result.saved) notify("备份已保存：" + result.name);
        }),
      );
      controls.append(
        button("从备份恢复", async () => {
          const selected = await api("team-service-restore-select");
          if (!selected.selected) return;
          modal("确认恢复协作服务", (body, actions) => {
            body.append(
              el("p", "", `备份文件：${selected.name}`),
              el(
                "p",
                "muted",
                `其中包含 ${selected.projects} 个项目、${selected.users} 个账户。恢复会用备份中的项目、成员、消息与任务替换当前协作服务数据。`,
              ),
            );
            note(
              body,
              "恢复前应用会自动保存当前数据库；本机 Agent 设置和项目工作目录不会被覆盖。恢复期间其他成员会短暂断开。",
            );
            actions.append(
              button("取消", closeModal),
              button(
                "确认恢复",
                async () => {
                  closeModal();
                  const result = await api("team-service-restore-apply", {
                    token: selected.token,
                  });
                  notify(
                    result.restored
                      ? "恢复完成，协作服务已重新启动。"
                      : "恢复未完成。",
                    !result.restored,
                  );
                  await update();
                },
                "primary",
              ),
            );
          });
        }),
      );
    }
    if (last.state === "needs_authorization") {
      const authorize = button(
        "打开 Tailscale 授权页面",
        () => action("team-service-authorize"),
        "primary",
      );
      authorize.disabled = !last.can_manage || !last.authorization_url;
      controls.append(authorize);
      note(
        card,
        "需要你在 Tailscale 页面确认启用此入口。确认后应用会自动检查状态，不会替你批准。",
      );
    }
    const stop = button("停止团队入口", () =>
      modal("停止团队入口", (b, a) => {
        note(
          b,
          "将停止同事通过这个入口连接本机工作台。不会删除账户、项目或任务，也不会关闭本机协作服务。",
        );
        a.append(
          button("取消", closeModal),
          button(
            "确认停止",
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
    controls.append(stop);
    controls.append(button("重新检查", update));
    card.append(controls);
    schedule(last);
  }
  // Paint after attachment; the independent poll updates only this card.
  card.refresh = update;
  card.paint = paint;
  return card;
}

Object.assign(HarnessDesktop.components, {
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
  const card = el("section", "panel");
  card.append(el("h2", "", "本机执行器"));
  note(
    card,
    "这台电脑装了哪些 CLI，以及哪些模型由它们执行。派来的任务按「模型」自动选择执行器：DeepSeek 模型走 DeepSeek Harness，GPT 模型走 Codex。两边都装了就能同机并行承担两种任务。",
  );
  const choice = field(
    card,
    "默认执行器（模型表未列出时使用）",
    current?.executor || "codex",
    [
      ["codex", "Codex CLI"],
      ["dsh", "DeepSeek Harness"],
    ],
  );
  choice.id = "executorSelect";

  const models = Array.isArray(current?.models)
    ? current.models.map((item) => ({ ...item }))
    : [];
  const rows = el("div");
  const draw = () => {
    rows.replaceChildren();
    models.forEach((item, index) => {
      const row = el("div", "setting-row");
      const name = el("input");
      name.type = "text";
      name.value = item.name || "";
      name.placeholder = "模型名，例如 deepseek-flash";
      name.oninput = () => {
        models[index].name = name.value.trim();
      };
      const kind = el("select");
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
      };
      row.append(
        name,
        kind,
        button("删除", () => {
          models.splice(index, 1);
          draw();
        }),
      );
      rows.append(row);
    });
  };
  draw();
  card.append(el("p", "muted small", "本机模型表"));
  card.append(rows);
  card.append(
    button("添加模型", () => {
      models.push({ name: "", executor: "codex" });
      draw();
    }),
  );
  note(
    card,
    "模型表是封闭的：派发时只认这里的模型。两边的完整模型清单无法离线枚举，所以把实际可用的模型填进来；填错不影响别的模型，派发到它时执行器会给出明确错误。",
  );
  const model = field(
    card,
    "DeepSeek Harness 模型（可留空）",
    current?.model || "",
  );
  model.id = "executorModel";
  model.placeholder = "留空则使用 Agent 上设置的模型；例如 deepseek-flash";
  const provider = field(
    card,
    "DeepSeek Harness 模型服务（可留空）",
    current?.provider || "",
  );
  provider.id = "executorProvider";
  provider.placeholder = "留空则使用 deepseek-official";
  const sync = () => {
    const dsh = choice.value === "dsh";
    [model, provider].forEach((input) => {
      input.disabled = !dsh;
      if (input.previousElementSibling)
        input.previousElementSibling.style.opacity = dsh ? "" : "0.5";
    });
  };
  choice.onchange = sync;
  sync();
  note(
    card,
    "DeepSeek Harness 的模型服务与 API Key 由它自己的配置决定，Harness 不会读取或保存密钥。思考强度按模型校验：medium 使用模型默认档，xhigh/max/ultra 按 high 发送。",
  );
  card.append(
    button(
      "保存执行器设置",
      async () => {
        const result = await api("executor-save", {
          executor: choice.value,
          model: model.value.trim(),
          provider: provider.value.trim(),
          models: models.filter((item) => item.name && item.name.trim()),
        });
        const status = result?.status;
        if (status?.found) {
          notify(
            "执行器已保存 · " +
              (choice.value === "dsh" ? "DeepSeek Harness " : "Codex ") +
              (status.version || "已检测到程序"),
          );
        } else {
          notify(
            "执行器已保存，但没有检测到可运行的程序：" +
              (status?.error || "请在下方手动指定"),
            true,
          );
        }
        await refresh(true);
      },
      "primary",
    ),
  );
  return card;
}

function proxySettingsCard(current) {
  const card = el("section", "panel");
  card.append(el("h2", "", "模型请求代理"));
  note(
    card,
    "仅影响这台电脑上 agent 的模型请求；团队服务和 Tailscale 不受影响。代理地址固定为 127.0.0.1。",
  );
  const row = el("label", "setting-row");
  const enabled = el("input", "check");
  enabled.type = "checkbox";
  enabled.checked = !!current?.enabled;
  row.append(el("span", "", "本机 Agent 使用本机代理"), enabled);
  card.append(row);
  const port = field(card, "代理端口", current?.port ?? "", null, "number");
  port.min = "1";
  port.max = "65535";
  port.placeholder = "例如 7897";
  const update = () => {
    port.disabled = !enabled.checked;
  };
  enabled.onchange = update;
  update();
  card.append(
    button(
      "保存代理设置",
      async () => {
        let value = null;
        if (enabled.checked) {
          const raw = port.value.trim();
          if (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) {
            notify("请输入 1 到 65535 的代理端口", true);
            port.focus();
            return;
          }
          value = Number(raw);
        }
        await api("proxy-save", {
          enabled: enabled.checked,
          port: value,
        });
        notify("代理设置已保存，下次启动的 agent 任务生效。");
        await refresh(true);
      },
      "primary",
    ),
  );
  return card;
}
function idleSettingsCard(seconds) {
  const card = el("section", "panel");
  card.append(el("h2", "", "Agent 空闲保活"));
  note(
    card,
    "有新消息时启动本机 Agent。任务结束后继续等待一段时间，期间的新消息复用同一进程；没有新消息时自动退出。",
  );
  const choice = field(card, "无新活动后退出", String(seconds || 300), [
    [60, "1 分钟"],
    [300, "5 分钟"],
    [900, "15 分钟"],
    [1800, "30 分钟"],
    [3600, "1 小时"],
  ]);
  card.append(
    button(
      "保存保活时间",
      async () => {
        await api("warm-idle-save", { seconds: Number(choice.value) });
        notify("保活时间已保存，下次启动的 Agent 生效。");
        await refresh(true);
      },
      "primary",
    ),
  );
  return card;
}
function codexCliCard(executor) {
  const dsh = executor === "dsh";
  const label = dsh ? "DeepSeek Harness CLI" : "Codex CLI";
  const card = el("section", "panel"),
    statusLine = el("p", "muted small", "正在检测 " + label + "…"),
    controls = el("div", "setting-row");
  statusLine.style.overflowWrap = "anywhere";
  card.append(el("h2", "", label), statusLine);
  note(
    card,
    dsh
      ? "DeepSeek Harness 需要在这台电脑上单独安装。这里只检测程序能否运行，不读取密钥或登录凭据。"
      : "Codex 需要在这台电脑上单独安装并登录。这里只读取 CLI 返回的登录状态，不会读取登录凭据。",
  );
  const path = field(card, "手动指定程序（留空则自动检测）", "");
  path.placeholder = dsh
    ? "选择 dsh（Mac/Linux）或 dsh.cmd（Windows）"
    : "选择 codex（Mac）或 codex.exe / codex.cmd（Windows）";
  const browse = button("浏览…", async () => {
    const r = await api("codex-cli-choose");
    if (r?.path) path.value = r.path;
  });
  const detect = button("重新检测", async () => {
    await load(true);
  });
  const save = button(
    "保存路径",
    async () => {
      const value = path.value.trim();
      const r = await api("codex-cli-save", { path: value });
      path.value = value ? r?.path || value : "";
      notify(value ? label + " 路径已保存。" : "已恢复自动检测。");
      await load();
    },
    "primary",
  );
  controls.append(detect, browse, save);
  card.append(controls);
  async function load(force = false) {
    detect.disabled = true;
    try {
      const r = await api(force ? "codex-cli-redetect" : "codex-cli-status");
      if (r?.found) {
        statusLine.className = r.logged_in ? "muted small" : "error small";
        statusLine.textContent =
          "已找到" +
          (r.version ? " · " + r.version : "") +
          (r.path ? " · " + r.path : "") +
          (r.logged_in
            ? " · " + (r.credential_warning || "已登录")
            : " · " + (r.login_error || "尚未登录"));
        if (r.configured && !path.value.trim() && r.path) path.value = r.path;
      } else {
        statusLine.className = "error small";
        statusLine.textContent =
          "未找到 " +
          label +
          (r?.error
            ? "：" + r.error
            : "。请安装后浏览选择 " + label + " 程序。");
      }
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = "检测失败：" + e.message;
    } finally {
      detect.disabled = false;
    }
  }
  card.refresh = load;
  load();
  return card;
}
function codexMcpCard(initial) {
  const card = el("section", "panel"),
    statusLine = el("p", "muted small", "正在读取连接状态…"),
    controls = el("div", "setting-row");
  const action = button(
    "连接到 Codex",
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
              ? "已断开本机连接；" + pending + " 个离线服务将在恢复后完成撤销。"
              : "已断开 Codex 协作连接。"
            : "已连接 Codex；普通 Codex 对话现在可以使用协作。",
          pending > 0,
        );
        await load(r?.codex_mcp || r);
      } catch (e) {
        notify("Codex 协作连接失败：" + e.message, true);
        action.disabled = false;
      }
    },
    "primary",
  );
  controls.append(action);
  card.append(el("h2", "", "在 Codex 中使用协作"), statusLine);
  note(
    card,
    "启用后，你可以在普通 Codex 对话里查看项目、发消息和派发异步任务。这里不会读取或显示登录凭据。",
  );
  card.append(controls);
  function paint(state) {
    state = state || {};
    card.state = state;
    const enabled = !!state.enabled,
      configured = !!state.configured;
    action.textContent =
      enabled && configured
        ? "断开 Codex"
        : enabled
          ? "重试连接"
          : "连接到 Codex";
    action.disabled = false;
    statusLine.className =
      enabled && configured ? "muted small" : "error small";
    statusLine.textContent = enabled
      ? configured
        ? "已启用 · Codex 已配置"
        : "已启用 · 尚未完成 Codex 配置"
      : "未启用";
    const old = card.querySelector(".codex-mcp-warnings");
    if (old) old.remove();
    const warnings = Array.isArray(state.warnings)
      ? state.warnings.filter(Boolean)
      : [];
    if (warnings.length) {
      const box = el("div", "note codex-mcp-warnings");
      box.append(el("strong", "", "需要注意："));
      warnings.forEach((w) =>
        box.append(
          el(
            "div",
            "",
            typeof w === "object"
              ? `${w.server || "协作服务"}：${w.error || "操作失败"}`
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
      statusLine.className = "error small";
      statusLine.textContent = "读取状态失败：" + e.message;
      action.disabled = false;
    }
  }
  card.refresh = load;
  paint(initial);
  if (!initial) load();
  return card;
}
function dshMcpCard(initial) {
  const card = el("section", "panel"),
    statusLine = el("p", "muted small", "正在读取连接状态…"),
    controls = el("div", "setting-row");
  statusLine.style.overflowWrap = "anywhere";
  const action = button(
    "连接到 DeepSeek Harness",
    async () => {
      const connected = !!card.state?.enabled && !!card.state?.configured;
      action.disabled = true;
      try {
        const state = await api(
          connected ? "dsh-mcp-disable" : "dsh-mcp-enable",
        );
        notify(
          connected
            ? "已断开 DeepSeek Harness 协作连接。"
            : "已连接；DeepSeek Harness 的会话现在可以使用协作工具。",
        );
        paint(state);
      } catch (e) {
        notify("DeepSeek Harness 协作连接失败：" + e.message, true);
      } finally {
        action.disabled = false;
      }
    },
    "primary",
  );
  controls.append(action);
  card.append(
    el("h2", "", "在 DeepSeek Harness 中使用协作"),
    statusLine,
    controls,
  );
  note(
    card,
    "在你的 DeepSeek Harness 桌面 profile 里登记一个 stdio MCP 服务，不开放端口，也不读取密钥。只影响桌面应用里的普通对话，被派发的 Agent 任务不受影响；Harness 只增删自己标记的两行，其它配置不动。新的 DSH 会话生效。",
  );
  function paint(state) {
    if (state) card.state = state;
    const s = card.state || {};
    const connected = !!s.enabled && !!s.configured;
    const stale = !!(s.configured && s.stale);
    action.textContent =
      connected && !stale
        ? "断开 DeepSeek Harness"
        : s.configured
          ? "重新连接"
          : "连接到 DeepSeek Harness";
    statusLine.className =
      s.configured && !stale ? "muted small" : "error small";
    statusLine.textContent = stale
      ? "已登记，但条目指向的启动命令已过期（应用升级后常见）；请点“重新连接”刷新"
      : s.configured
        ? "已启用 · " + (s.path || "profile 补丁层")
        : s.enabled
          ? "已授权，但配置中缺少入口；请重新连接"
          : "未连接";
  }
  async function load(state) {
    if (state) return paint(state);
    try {
      paint(await api("dsh-mcp-status"));
    } catch (e) {
      statusLine.className = "error small";
      statusLine.textContent = "读取状态失败：" + e.message;
    }
  }
  card.refresh = load;
  paint(initial);
  if (!initial) load();
  return card;
}
