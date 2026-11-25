"use strict";
function renderDashboard() {
  S.epoch++;
  nav("dashboard");
  S.project = null;
  const c = $("content");
  c.replaceChildren(head("控制台"));
  const heading = el("div", "section-head");
  heading.append(
    el("h2", "", "我的项目"),
    button("＋ 新建项目", newProject, "primary"),
  );
  const projects = el("div", "panel");
  if (!S.dash.projects.length)
    empty(projects, "创建项目，或凭邀请加入合作伙伴的项目。");
  for (const project of S.dash.projects) {
    const entry = button("", () => openProject(project.id), "project");
    entry.append(
      el("div", "project-name", project.name + "　›"),
      el(
        "div",
        "muted small",
        "上次进入对话 · " +
          date(project.last_entered) +
          (S.dash.services?.length > 1
            ? " · " + serviceName(project.server)
            : ""),
      ),
    );
    projects.append(entry);
  }
  c.append(heading, projects, button("凭邀请加入项目", joinProject, "link"));
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
Object.assign(FoolshipDesktop.render, { renderDashboard });
Object.assign(FoolshipDesktop.components, { newProject, joinProject });
