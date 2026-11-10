"use strict";
function renderDashboard() {
  S.epoch++;
  nav("dashboard");
  S.project = null;
  const c = $("content");
  c.replaceChildren(head("控制台", "管理你的员工，继续项目中的讨论。"));
  const cols = el("div", "columns"),
    left = el("section"),
    right = el("section"),
    lh = el("div", "section-head");
  lh.append(el("h2", "", "我的 Agents"), button("＋ 添加员工", newEmployee));
  left.append(lh);
  const ap = el("div", "panel"),
    fr = el("div", "row");
  fr.append(el("span", "muted small", "工作目录属于员工所在机器"));
  const f = el("select", "filter");
  [
    ["all", "全部机器"],
    ["local", "仅当前机器"],
  ].forEach(([v, t]) => {
    const o = el("option", "", t);
    o.value = v;
    f.append(o);
  });
  f.value = S.filter;
  f.onchange = () => {
    S.filter = f.value;
    renderDashboard();
  };
  fr.append(f);
  ap.append(fr);
  const staff = S.dash.employees.filter(
    (a) =>
      S.filter === "all" ||
      S.dash.devices.some(
        (d) =>
          d.local &&
          (d.node || d.id) === a.node &&
          (d.server === employeeServer(a) ||
            (!d.server && employeeServer(a) === S.server)),
      ),
  );
  if (!staff.length)
    empty(ap, "还没有员工。先关联机器，再添加你的第一个员工。");
  staff.forEach((a) => {
    const healthIssue = a.health?.components?.find(
      (item) => item && item.code && item.code !== "OK",
    );
    const card = el("div", "agent"),
      r = el("div", "row"),
      i = el("div", "agent-name"),
      text = el("div");
    text.append(
      el("strong", "", a.name),
      el(
        "div",
        a.pending?.length > 0 ? "pending small" : "muted small",
        a.pending?.length > 0
          ? "等待目标设备确认"
          : a.last_error || healthIssue?.message || a.status || "待配置",
      ),
    );
    i.append(el("span", "bot", "◇"), text);
    r.append(
      i,
      button("设置", () => employeeSettings(a)),
      button("删除", () => deleteEmployee(a)),
    );
    card.append(
      r,
      el(
        "div",
        "agent-meta",
        [
          a.model || "未设置模型",
          deviceName(a.node, employeeServer(a)),
          serviceName(employeeServer(a)),
          a.enabled ? "已启用" : "未启用",
          a.status_code && a.status_code !== "OK" ? a.status_code : "运行正常",
        ].join(" · "),
      ),
    );
    ap.append(card);
  });
  left.append(ap);
  const rh = el("div", "section-head");
  rh.append(
    el("h2", "", "我的项目"),
    button("＋ 新建项目", newProject, "primary"),
  );
  right.append(rh);
  const pp = el("div", "panel");
  if (!S.dash.projects.length)
    empty(pp, "创建项目，或凭邀请加入合作伙伴的项目。");
  S.dash.projects.forEach((p) => {
    const b = button("", () => openProject(p.id), "project");
    b.append(
      el("div", "project-name", p.name + "　›"),
      el(
        "div",
        "muted small",
        "上次进入对话 · " +
          date(p.last_entered) +
          (S.dash.services?.length > 1 ? " · " + serviceName(p.server) : ""),
      ),
    );
    pp.append(b);
  });
  right.append(pp, button("凭邀请加入项目", joinProject, "link"));
  cols.append(right, left);
  c.append(cols);
  renderApprovals(c);
  renderAttachmentApprovals(c);
}

function renderAttachmentApprovals(parent) {
  const requests = S.dash.attachment_approvals || [];
  if (!requests.length) return;
  const panel = el("section", "panel");
  panel.style.marginTop = "24px";
  panel.append(el("h2", "", "等待你的附件发送批准"));
  requests.forEach((request) => {
    const row = el("div", "setting-row"),
      info = el("div");
    info.append(
      el("strong", "", `${request.agent_name} · ${request.project_name}`),
      el(
        "p",
        "muted small",
        `${request.manifest.length} 个文件 · ${deviceName(request.node, request.server)}`,
      ),
    );
    row.append(
      info,
      button("审阅", () =>
        modal("批准 Agent 发送附件", (body, actions) => {
          body.append(
            el(
              "p",
              "muted small",
              `项目：${request.project_name} · Agent：${request.agent_name} · 发送到：${request.destination}`,
            ),
          );
          request.manifest.forEach((file) => {
            body.append(el("p", "", `${file.path} · ${fileSize(file.size)}`));
          });
          note(
            body,
            "批准后，目标机器会再次核对文件内容；文件有变化时不会发送。",
          );
          const decide = async (approve) => {
            await api("attachment-decide", {
              id: request.id,
              approve,
              server: request.server,
            });
            closeModal();
            await refresh(true);
          };
          actions.append(
            button("拒绝", () => decide(false)),
            button("批准发送", () => decide(true), "primary"),
          );
        }),
      ),
    );
    panel.append(row);
  });
  parent.append(panel);
}

function newProject() {
  modal("新建项目", (b, a) => {
    const name = field(b, "项目名称");
    const local = "http://127.0.0.1:8765";
    const services = (S.dash.services || []).filter(
      (s) => s.hosted_by_me === true,
    );
    if (!services.some((s) => s.server === local))
      services.unshift({ server: local });
    const service = field(
      b,
      "项目所在的协作服务",
      services[0].server,
      services.map((s) => [
        s.server,
        s.server === local ? "这台机器" : serviceName(s.server),
      ]),
    );
    note(
      b,
      "选择“这台机器”时，应用会自动建立 data 数据目录并准备本机协作服务。其他选项只显示你自己名下的服务。",
    );
    a.append(
      button("取消", closeModal),
      button(
        "创建并进入",
        async () => {
          if (!name.value.trim()) throw Error("请填写项目名称");
          const r = await api("new-project", {
            name: name.value.trim(),
            server: service.value,
          });
          closeModal();
          S.dash = await api("dashboard");
          await openProject(r.id || r.project?.id);
        },
        "primary",
      ),
    );
  });
}
function joinProject() {
  modal("加入项目", (b, a) => {
    const server = field(b, "协作服务地址", S.server),
      invite = field(b, "项目邀请代码");
    note(
      b,
      "使用群主提供的服务地址和项目邀请代码。你的现有身份会加入该项目，无需新建账户。服务托管者会保存这个项目的消息；请先确认地址来自你信任的群主。",
    );
    a.append(
      button("取消", closeModal),
      button(
        "加入",
        async () => {
          if (!server.value.trim() || !invite.value.trim())
            throw Error("请填写服务地址和项目邀请代码");
          const r = await api("join-project", {
            server: server.value.trim(),
            invite: invite.value.trim(),
          });
          closeModal();
          notify(
            "已加入 " + serviceName(r.server || server.value) + " 上的项目。",
          );
          await refresh(true);
        },
        "primary",
      ),
    );
  });
}
function newEmployee() {
  modal("添加员工", (b, a) => {
    const name = field(b, "员工名称");
    const services = S.dash.services || [
      { server: S.server, primary: true, attached: true },
    ];
    const service = field(
      b,
      "协作服务",
      "",
      services.map((s) => [
        s.server,
        serviceName(s.server) + (s.attached ? "" : " · 本机尚未接入"),
      ]),
    );
    const machine = field(b, "所在机器", "", []);
    function updateMachines() {
      const current = service.value;
      machine.replaceChildren();
      (S.dash.devices || [])
        .filter(
          (d) => d.server === current || (!d.server && current === S.server),
        )
        .forEach((d) => {
          const o = el(
            "option",
            "",
            d.name +
              (d.local ? " · 本机" : "") +
              (d.online ? " · 在线" : " · 离线"),
          );
          o.value = d.node || d.id;
          machine.append(o);
        });
      add.disabled = !machine.options.length;
    }
    service.onchange = updateMachines;
    note(
      b,
      "员工属于所选协作服务。先在设置中将本机接入该服务，才可派遣本机员工参加其中的项目。",
    );
    const add = button(
      "添加员工",
      async () => {
        if (!name.value.trim() || !machine.value)
          throw Error("请填写员工名称并选择机器");
        await api("employee-create", {
          name: name.value.trim(),
          node: machine.value,
          server: service.value,
        });
        closeModal();
        notify("创建请求已提交，等待目标设备确认。");
        await refresh(true);
      },
      "primary",
    );
    updateMachines();
    a.append(button("取消", closeModal), add);
  });
}
function deleteEmployee(e) {
  modal("删除员工 · " + e.name, (b, actions) => {
    b.append(el("p", "muted", "正在核对派遣项目与未完成任务…"));
    actions.append(button("取消", closeModal));
    run(async () => {
      const preview = await api("employee-delete-preview", { id: e.id });
      if (!$("modalBody").contains(b)) return;
      b.replaceChildren();
      note(
        b,
        "确认后会从所有项目撤回该员工并停止接收新任务；工作文件、聊天、会话和任务历史会保留。离线设备会在上线后应用删除。",
      );
      if (preview.projects.length) {
        b.append(el("h3", "", "将撤回的项目"));
        preview.projects.forEach((project) =>
          b.append(
            el(
              "div",
              "member",
              project.name +
                (project.administrator
                  ? " · 当前管理员（删除后将清空管理员）"
                  : ""),
            ),
          ),
        );
      } else note(b, "该员工尚未派遣到任何项目。");
      if (preview.active_tasks.length) {
        b.append(el("h3", "", "尚未结束的任务"));
        preview.active_tasks.forEach((task) =>
          b.append(
            el(
              "div",
              "task pending",
              (task.title || task.id) + " · " + task.state,
            ),
          ),
        );
        note(
          b,
          "请先等待这些任务结束或在项目中停止它们，再重新删除。应用不会强行中断并丢失工作。 ",
        );
      }
      actions.replaceChildren(button("取消", closeModal));
      const confirm = button(
        "确认删除员工",
        async () => {
          await api("employee-delete", { id: e.id });
          closeModal();
          notify("删除请求已提交，等待目标设备确认。");
          await refresh(true);
        },
        "primary",
      );
      confirm.disabled = !preview.can_delete;
      actions.append(confirm);
    });
  });
}
function employeeSettings(e) {
  // The model decides which local CLI runs the turn, so it is chosen from this
  // machine's table instead of being typed freely.
  api("settings-get")
    .catch(() => null)
    .then((settings) =>
      openEmployeeSettings(e, settings?.executor?.models || []),
    );
}

function openEmployeeSettings(e, models) {
  modal("员工设置 · " + e.name, (b, a) => {
    const name = field(b, "员工名称", e.name);
    const modelRow = el("label", "setting-row");
    const model = el("select");
    const groups = { codex: [], dsh: [] };
    (models.length
      ? models
      : [{ name: e.model || "gpt-5.6-sol", executor: "codex" }]
    ).forEach((item) => {
      (groups[item.executor === "dsh" ? "dsh" : "codex"] || groups.codex).push(
        item,
      );
    });
    [
      ["codex", "Codex CLI"],
      ["dsh", "DeepSeek Harness"],
    ].forEach(([kind, label]) => {
      if (!groups[kind].length) return;
      const group = el("optgroup");
      group.label = label;
      groups[kind].forEach((item) => {
        const option = el("option", "", item.name);
        option.value = item.name;
        group.append(option);
      });
      model.append(group);
    });
    model.value = e.model || models[0]?.name || "gpt-5.6-sol";
    modelRow.append(el("span", "", "模型"), model);
    b.append(modelRow);
    if (!models.length)
      b.append(
        el(
          "p",
          "muted small",
          "本机还没有配置模型表；请先到设置 → 本机执行器里添加模型。",
        ),
      );
    const effort = field(
      b,
      "思考强度",
      e.effort || "medium",
      ["low", "medium", "high", "xhigh", "max", "ultra"].map((v) => [v, v]),
    );
    const enabled = field(b, "接收项目任务", String(!!e.enabled), [
      ["false", "暂停"],
      ["true", "启用"],
    ]);
    const stages = [];
    const stageBox = el("div");
    stageBox.append(el("label", "label", "允许的工作阶段"));
    ["discuss", "review", "implement"].forEach((s, i) => {
      const l = el("label", "setting-row", ["讨论", "审阅", "实现"][i]),
        check = el("input", "check");
      check.type = "checkbox";
      check.checked = (e.stages || ["discuss", "review"]).includes(s);
      l.append(check);
      stageBox.append(l);
      stages.push([s, check]);
    });
    b.append(stageBox);
    const timeout = field(
      b,
      "每轮超时（秒）",
      e.timeout || 600,
      null,
      "number",
    );
    timeout.min = "1";
    const strict = field(b, "实现任务安全模式", String(!!e.strict_worktree), [
      ["false", "关闭"],
      ["true", "仅允许 linked worktree 且限制变更文件"],
    ]);
    const attachmentPolicy = field(
      b,
      "附件发送权限",
      e.attachment_policy || "ask",
      [
        ["ask", "请求批准"],
        ["auto", "帮我批准"],
        ["full", "完全访问权限"],
      ],
    );
    note(
      b,
      "此设置控制 Agent 将当前项目工作目录中的文件作为聊天附件发送。目录外文件始终不能自动发送。",
    );
    note(
      b,
      "模型与执行权限由目标机器应用。工作目录改为在项目派遣页面按机器设置，不再属于某个员工。",
    );
    const network = field(
      b,
      "模型请求代理",
      (e.network && e.network.mode) || "machine",
      [
        ["machine", "跟随本机设置"],
        ["direct", "直连（不走代理）"],
        ["custom", "自定义端口"],
      ],
    );
    const networkPort = field(
      b,
      "代理端口",
      (e.network && e.network.port) || "",
    );
    networkPort.placeholder = "例如 7897";
    const syncNetwork = () => {
      networkPort.disabled = network.value !== "custom";
    };
    network.onchange = syncNetwork;
    syncNetwork();
    a.append(
      button("取消", closeModal),
      button(
        "保存设置",
        async () => {
          await api("employee-configure", {
            id: e.id,
            name: name.value.trim(),
            model: model.value.trim(),
            effort: effort.value,
            enabled: enabled.value === "true",
            stages: stages.filter(([, c]) => c.checked).map(([s]) => s),
            timeout: Number(timeout.value),
            strict_worktree: strict.value === "true",
            attachment_policy: attachmentPolicy.value,
            network:
              network.value === "custom"
                ? { mode: "custom", port: Number(networkPort.value) || 0 }
                : { mode: network.value },
          });
          closeModal();
          notify("设置请求已提交，等待目标设备应用。");
          await refresh(true);
        },
        "primary",
      ),
    );
  });
}
Object.assign(FoolshipDesktop.render, { renderDashboard });
Object.assign(FoolshipDesktop.components, {
  newProject,
  joinProject,
  newEmployee,
  employeeSettings,
});
