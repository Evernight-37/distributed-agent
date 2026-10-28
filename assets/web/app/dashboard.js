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
    note(
      b,
      "将移除该员工并撤回所有项目派遣；工作文件、会话和任务历史会保留。若它是管理员，群主需要另选管理员。未完成的任务需先结束；离线设备会在上线后确认删除。",
    );
    actions.append(
      button("取消", closeModal),
      button(
        "确认删除员工",
        async () => {
          await api("employee-delete", { id: e.id });
          closeModal();
          notify("删除请求已提交，等待目标设备确认。");
          await refresh(true);
        },
        "primary",
      ),
    );
  });
}
function employeeSettings(e) {
  modal("员工设置 · " + e.name, (b, a) => {
    const name = field(b, "员工名称", e.name),
      model = field(b, "模型", e.model || "gpt-5.6-sol");
    model.setAttribute("list", "models");
    const dl = el("datalist");
    dl.id = "models";
    ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-5.6-luna"].forEach((v) => {
      const o = el("option");
      o.value = v;
      dl.append(o);
    });
    b.append(dl);
    const effort = field(
        b,
        "思考强度",
        e.effort || "medium",
        ["low", "medium", "high", "xhigh", "max", "ultra"].map((v) => [v, v]),
      ),
      path = field(
        b,
        "工作目录 · " + deviceName(e.node, employeeServer(e)),
        e.path || "",
      );
    const local = S.dash.devices.some(
      (d) =>
        d.local &&
        (d.node || d.id) === e.node &&
        (d.server === employeeServer(e) ||
          (!d.server && employeeServer(e) === S.server)),
    );
    b.append(
      button(local ? "选择文件夹" : "浏览目标机器文件夹", async () => {
        if (local) {
          const r = await api("choose-folder");
          if (r.path) path.value = r.path;
        } else await remoteFolders(e.node, path, employeeServer(e));
      }),
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
    note(
      b,
      "模型、目录和权限由目标机器应用。远端目录不会复制到本机；保存后请等待目标机器确认。",
    );
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
            path: path.value,
            enabled: enabled.value === "true",
            stages: stages.filter(([, c]) => c.checked).map(([s]) => s),
            timeout: Number(timeout.value),
            strict_worktree: strict.value === "true",
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
async function remoteFolders(node, input, server) {
  const box = el("div", "note");
  $("modalBody").append(box);
  async function browse(path) {
    box.replaceChildren(el("p", "", "正在请求目标机器目录…"));
    const r = await api("browse-folders", { node, path, server });
    let result = r;
    for (let i = 0; r.request_id && i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (!$("modalBody").contains(box)) return;
      result = await api("command-status", { id: r.request_id, server });
      if (["failed", "error"].includes(result.state))
        throw Error(result.error || result.result?.error || "目录浏览失败");
      if (
        ["completed", "done", "succeeded", "applied"].includes(result.state)
      ) {
        result = result.result;
        break;
      }
    }
    box.replaceChildren();
    if (
      result.request_id ||
      ["pending", "queued", "claimed"].includes(result.state)
    ) {
      box.append(
        el("p", "", "目标设备尚未回应。也可直接填写该机器的现有路径。"),
      );
      return;
    }
    const dirs =
      result.dirs ||
      result.directories ||
      result.children ||
      result.roots ||
      [];
    if (result.parent)
      box.append(button("↑ 上级目录", () => browse(result.parent)));
    if (result.path)
      box.append(
        button(
          "使用此目录",
          () => {
            input.value = result.path;
            box.remove();
          },
          "primary",
        ),
      );
    dirs.forEach((d) =>
      box.append(button(d.name || d.path, () => browse(d.path))),
    );
    if (!dirs.length && !result.path)
      box.append(el("p", "", "未返回目录，请直接填写目标机器上的路径。"));
  }
  await browse(input.value || undefined);
}

Object.assign(HarnessDesktop.render, { renderDashboard });
Object.assign(HarnessDesktop.components, {
  newProject,
  joinProject,
  newEmployee,
  employeeSettings,
});
