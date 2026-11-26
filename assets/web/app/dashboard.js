"use strict";
function renderDashboard() {
  S.epoch++;
  nav("dashboard");
  S.project = null;
  const c = $("content");
  const page = el("div", "dashboard-home");
  const header = el("header", "dashboard-heading");
  const actions = el("div", "dashboard-actions");
  actions.append(
    button(t("加入项目", "Join project"), joinProject),
    button(t("＋ 新建项目", "＋ New project"), newProject, "primary"),
  );
  header.append(head(t("控制台", "Home")), actions);
  page.append(header);
  if (S.dash.unavailable_services?.length)
    page.append(
      el(
        "p",
        "error",
        t(
          "部分协作服务暂时无法连接；其项目会在恢复连接后显示。",
          "Some collaboration services are unavailable. Their projects will return once the connection is restored.",
        ),
      ),
    );
  const approvals = el("div", "dashboard-approvals");
  renderApprovals(approvals);
  renderAttachmentApprovals(approvals);
  if (approvals.childElementCount) page.append(approvals);

  const active = S.dash.projects.filter((project) => !project.archived_at);
  const archived = S.dash.projects.filter((project) => project.archived_at);
  const section = el("section", "dashboard-projects");
  const heading = el("div", "section-head");
  heading.append(
    el("h2", "", t("我的项目", "My projects")),
    el("span", "muted small", String(active.length)),
  );
  const projects = el("div", "dashboard-project-grid");
  if (!active.length) {
    projects.classList.add("panel", "dashboard-empty");
    empty(
      projects,
      t(
        "创建项目，或凭邀请加入合作伙伴的项目。",
        "Create a project or join one with an invite.",
      ),
    );
  }
  active.forEach((project) => projects.append(dashboardProjectEntry(project)));
  section.append(heading, projects);
  page.append(section);
  if (archived.length) {
    const previous = c.querySelector("#archivedProjects");
    const archive = el(
      "details",
      "panel side-section dashboard-archived-projects",
    );
    archive.id = "archivedProjects";
    archive.open = previous?.open || false;
    archive.append(
      el(
        "summary",
        "",
        `${t("已归档项目", "Archived projects")} (${archived.length})`,
      ),
    );
    const list = el("div", "dashboard-project-grid");
    archived.forEach((project) => list.append(dashboardProjectEntry(project)));
    archive.append(list);
    page.append(archive);
  }
  c.replaceChildren(page);
}

function dashboardProjectEntry(project) {
  const entry = button("", () => openProject(project.id), "project");
  const mark = el(
    "span",
    "project-mark",
    Array.from(project.name.trim())[0] || t("项", "P"),
  );
  mark.setAttribute("aria-hidden", "true");
  const info = el("div");
  info.append(el("div", "project-name", project.name));
  const context = [
    S.dash.services?.length > 1 ? serviceName(project.server) : "",
    project.archived_at
      ? t(
          `归档于 ${date(project.archived_at)}`,
          `Archived ${date(project.archived_at)}`,
        )
      : project.last_entered
        ? t(
            `最近打开 ${date(project.last_entered)}`,
            `Last opened ${date(project.last_entered)}`,
          )
        : "",
  ].filter(Boolean);
  if (context.length)
    info.append(el("div", "muted small", context.join(" · ")));
  const arrow = el("span", "project-arrow", "›");
  arrow.setAttribute("aria-hidden", "true");
  entry.append(mark, info, arrow);
  return entry;
}

function renderAttachmentApprovals(parent) {
  const requests = S.dash.attachment_approvals || [];
  if (!requests.length) return;
  const panel = el("section", "panel");
  panel.append(el("h2", "", t("附件待审批", "Attachments awaiting approval")));
  requests.forEach((request) => {
    const row = el("div", "setting-row"),
      info = el("div");
    const count = request.manifest.length;
    const device = deviceName(request.node, request.server);
    info.append(
      el("strong", "", `${request.agent_name} · ${request.project_name}`),
      el(
        "p",
        "muted small",
        t(
          `${count} 个文件 · ${device}`,
          `${count} ${count === 1 ? "file" : "files"} · ${device}`,
        ),
      ),
    );
    row.append(
      info,
      button(t("审阅", "Review"), () =>
        modal(
          t("批准 Agent 发送附件", "Approve agent attachments"),
          (body, actions) => {
            body.append(
              el(
                "p",
                "muted small",
                t(
                  `项目：${request.project_name} · Agent：${request.agent_name} · 发送到：${request.destination}`,
                  `Project: ${request.project_name} · Agent: ${request.agent_name} · To: ${request.destination}`,
                ),
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
              button(t("拒绝", "Deny"), () => decide(false)),
              button(
                t("批准发送", "Approve sending"),
                () => decide(true),
                "primary",
              ),
            );
          },
        ),
      ),
    );
    panel.append(row);
  });
  parent.append(panel);
}

function newProject() {
  modal(t("新建项目", "New project"), (b, a) => {
    const name = field(b, t("项目名称", "Project name"));
    const local = "http://127.0.0.1:8765";
    const services = (S.dash.services || []).filter(
      (s) => s.hosted_by_me === true,
    );
    if (!services.some((s) => s.server === local))
      services.unshift({ server: local });
    const service = field(
      b,
      t("项目所在的协作服务", "Collaboration service"),
      services[0].server,
      services.map((s) => [
        s.server,
        s.server === local
          ? t("这台机器", "This computer")
          : serviceName(s.server),
      ]),
    );
    a.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("创建并进入", "Create and open"),
        async () => {
          if (!name.value.trim())
            throw Error(t("请填写项目名称", "Enter a project name."));
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
  modal(t("加入项目", "Join project"), (b, a) => {
    const server = field(
        b,
        t("协作服务地址", "Collaboration service address"),
        S.server,
      ),
      invite = field(b, t("项目邀请代码", "Project invite code"));
    a.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("加入", "Join"),
        async () => {
          if (!server.value.trim() || !invite.value.trim())
            throw Error(
              t(
                "请填写服务地址和项目邀请代码",
                "Enter the service address and project invite code.",
              ),
            );
          const r = await api("join-project", {
            server: server.value.trim(),
            invite: invite.value.trim(),
          });
          closeModal();
          const service = serviceName(r.server || server.value);
          notify(
            t(
              `已加入 ${service} 上的项目。`,
              `Joined a project on ${service}.`,
            ),
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
