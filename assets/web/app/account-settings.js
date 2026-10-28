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
  account.append(button("添加 Linux 服务器", linuxServerSetup));
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
  const memberships = el("section", "panel");
  memberships.append(el("h2", "", "已加入的协作服务"));
  note(
    memberships,
    "同一身份可以参与多个服务上的项目。接入本机 agent 是单独操作，项目工作目录不会自动同步。",
  );
  (
    settings.services || [
      { server: settings.server, primary: true, attached: true },
    ]
  ).forEach((s) => {
    const row = el("div", "setting-row");
    row.append(
      el(
        "div",
        "",
        serviceName(s.server) +
          (s.primary ? " · 首个服务" : "") +
          (s.attached ? " · 本机已接入" : ""),
      ),
    );
    if (!s.attached)
      row.append(
        button("接入本机 agent", () =>
          modal("接入本机 agent", (b, a) => {
            note(
              b,
              "将这台机器的任务接收端接入 " +
                s.server +
                "。此后可在该服务创建和派遣本机员工。",
            );
            a.append(
              button("取消", closeModal),
              button(
                "确认接入",
                async () => {
                  await api("receiver-attach", { server: s.server });
                  closeModal();
                  notify("本机已接入 " + serviceName(s.server));
                  await refresh(true);
                },
                "primary",
              ),
            );
          }),
        ),
      );
    memberships.append(row);
  });
  list.append(memberships);
  const machine = el("section", "panel");
  machine.append(el("h2", "", "本机接收任务"));
  note(
    machine,
    "点击窗口关闭按钮时会询问退出方式：继续后台运行，或完全退出。停止接收仅停止任务接收服务；完全退出还会停止任务接收服务启动的本机协作服务。",
  );
  const controls = el("div", "setting-row");
  controls.append(
    button(
      "启动接收",
      async () => {
        await api("receiver-start");
        notify("本机接收服务已启动");
        await refresh(true);
      },
      "primary",
    ),
    button("停止接收", async () => {
      await api("receiver-stop");
      notify("接收服务已停止；已启动的作业独立运行。");
      await refresh(true);
    }),
  );
  machine.append(controls, button("完全退出", requestAppClose));
  list.append(
    machine,
    proxySettingsCard(settings.proxy),
    idleSettingsCard(settings.warm_idle_seconds),
    codexCliCard(),
    codexMcpCard(settings.codex_mcp),
  );
  const storage = el("section", "panel");
  storage.append(
    el("h2", "", "本机数据"),
    el("p", "muted small", "本机数据位置：" + settings.data_root),
    el(
      "p",
      "muted small",
      "项目工作目录不自动同步。模型凭据和运行环境也保留在各自机器。",
    ),
  );
  list.append(storage);
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
    "此地址是创建身份时接入的首个服务。加入其他服务的项目，请在控制台使用项目邀请。",
  );
  list.append(advanced);
  c.append(list);
  renderApprovals(c);
  if (settings.team_service) team.paint(settings.team_service);
  else await team.refresh();
}

function linuxServerSetup() {
  const services = (S.dash.services || []).filter((item) =>
    String(item.server || "").startsWith("https://"),
  );
  modal("添加 Linux 服务器", (body, actions) => {
    note(
      body,
      "把生成的提示词粘贴给服务器上的 Codex。它会检查环境并完成 Harness 配置，只在 Tailscale 授权、设备批准和系统权限等节点请你确认。",
    );
    if (!services.length) {
      note(
        body,
        "当前账户还没有可供服务器访问的 HTTPS 团队入口。请先在设置 → 协作服务中创建团队入口。",
      );
      actions.append(button("关闭", closeModal));
      return;
    }
    const service = field(
      body,
      "服务器要接入的协作服务",
      services[0].server,
      services.map((item) => [item.server, serviceName(item.server)]),
    );
    const output = document.createElement("textarea");
    output.readOnly = true;
    output.rows = 18;
    output.placeholder = "正在生成安全配置提示词…";
    body.append(output);
    const copy = button(
      "复制给服务器 Codex",
      async () => {
        if (!output.value) throw new Error("提示词尚未生成");
        await copyText(output.value, output);
        notify("Linux 配置提示词已复制");
      },
      "primary",
    );
    copy.disabled = true;
    async function generate() {
      copy.disabled = true;
      output.value = "";
      const result = await api("linux-setup-prompt", {
        server: service.value,
      });
      output.value = result.prompt;
      copy.disabled = false;
    }
    service.onchange = () => run(generate);
    actions.append(button("关闭", closeModal), copy);
    run(generate);
  });
}

Object.assign(HarnessDesktop.render, { renderAccount, renderSettings });
Object.assign(HarnessDesktop.components, { linuxServerSetup });
