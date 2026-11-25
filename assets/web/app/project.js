"use strict";
async function openProject(id) {
  const epoch = ++S.epoch;
  S.project = id;
  S.chatSelection = S.chatSelection || new Map();
  S.chatSelection.delete(id);
  const view = await api("project", { id, visit: true });
  if (epoch !== S.epoch || S.project !== id) return;
  S.view = view;
  const group = (view.conversations || []).find((x) => x.kind === "group");
  if (group) S.chatSelection.set(id, group.id);
  renderProject();
}

function streamMessageText(message, node, box) {
  const key = S.project + ":" + message.id,
    partial = String(message.causal_task || "").startsWith("draft:"),
    previous = S.streamTexts.get(key);
  if (!partial && !previous) return;
  const entry = previous || { shown: "", target: "", node, box, partial };
  entry.target = message.body;
  entry.node = node;
  entry.box = box;
  entry.partial = partial;
  if (!entry.target.startsWith(entry.shown)) entry.shown = "";
  node.textContent = entry.shown;
  S.streamTexts.set(key, entry);
  if (streamTimer) return;
  streamTimer = setInterval(() => {
    let active = false;
    for (const [id, item] of S.streamTexts) {
      if (!document.body.contains(item.node)) continue;
      const chars = Array.from(item.target),
        count = Array.from(item.shown).length;
      if (count < chars.length) {
        active = true;
        const stick =
          item.box.scrollHeight - item.box.scrollTop - item.box.clientHeight <
          36;
        item.shown += chars
          .slice(
            count,
            count + Math.max(1, Math.ceil((chars.length - count) / 25)),
          )
          .join("");
        item.node.textContent = item.shown;
        if (stick) item.box.scrollTop = item.box.scrollHeight;
      } else if (!item.partial) S.streamTexts.delete(id);
    }
    if (!active) {
      clearInterval(streamTimer);
      streamTimer = null;
    }
  }, 22);
}
function conversationKind(conversation) {
  return conversation.kind || conversation.type || "group";
}
function conversationParticipants(conversation) {
  return (conversation.participants || []).map((p) =>
    typeof p === "object" ? p.id || p.agent_id || p.user_id : p,
  );
}
function projectClientRequestId() {
  return (
    globalThis.crypto?.randomUUID?.() ||
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = Math.floor(Math.random() * 16);
      return (c === "x" ? r : (r & 3) | 8).toString(16);
    })
  );
}
function projectWorkRequest(action, fields = {}, clientRequestId) {
  return api("project-work", {
    project: S.project,
    action,
    client_request_id: clientRequestId || projectClientRequestId(),
    ...fields,
  });
}
function projectWorkHandler(action, fields, complete) {
  const clientRequestId = projectClientRequestId();
  let pending = false;
  return async () => {
    if (pending) return;
    pending = true;
    try {
      await projectWorkRequest(action, fields(), clientRequestId);
      await complete?.();
    } finally {
      pending = false;
    }
  };
}
async function refreshProjectWork(project = S.project) {
  if (!project || S.project !== project) return;
  const view = await api("project", { id: project, visit: false });
  if (S.project !== project) return;
  S.view = view;
  renderProject();
}
function projectWorkFields(body, view, task = {}) {
  return {
    title: field(body, "标题", task.title || ""),
    instructions: field(body, "工作说明", task.instructions || task.body || ""),
    deliverable: field(body, "交付物", task.deliverable || ""),
    acceptance: field(body, "验收标准", task.acceptance || ""),
    validation: field(body, "验证命令", task.validation || ""),
    allowed_files: field(body, "允许修改的文件", task.allowed_files || ""),
    agent: field(body, "负责人", task.agent || task.agent_id || "", [
      ["", "选择 Agent"],
      ...(view.members || []).map((agent) => [
        agent.id,
        agent.name || agent.id,
      ]),
    ]),
    stage: field(body, "阶段", task.stage || "implement", [
      ["discuss", "讨论"],
      ["implement", "实现"],
    ]),
    visibility: field(body, "可见范围", task.visibility || "group", [
      ["group", "项目成员"],
      ["private", "仅相关成员"],
    ]),
  };
}
function projectWorkValues(controls) {
  return Object.fromEntries(
    Object.entries(controls).map(([key, control]) => [
      key,
      String(control.value).trim(),
    ]),
  );
}
function openProjectWorkForm(view, options = {}) {
  const source = options.draft || options.task || {},
    project = view.project.id;
  modal(options.draft ? "审阅工作草稿" : "创建项目工作", (body, actions) => {
    const controls = projectWorkFields(body, view, source);
    if (options.draft || source.visibility === "private")
      controls.visibility.disabled = true;
    if (!options.draft && source.visibility === "private" && source.agent)
      controls.agent.disabled = true;
    const initial = JSON.stringify(projectWorkValues(controls));
    const changed = () =>
      JSON.stringify(projectWorkValues(controls)) !== initial;
    appendProjectMessageReferences(
      body,
      view,
      options.draft?.reference_message_ids || [],
      "草稿依据",
    );
    actions.append(button("取消", closeModal));
    if (options.draft) {
      note(
        body,
        "草稿版本 " +
          options.draft.revision +
          "。修改后须先保存新版本，不能直接确认。",
      );
      if (options.draft.can_edit === false) {
        Object.values(controls).forEach((control) => (control.disabled = true));
        note(body, "你可以查看此草稿，但不能修改或确认。");
        return;
      }
      actions.append(
        button("继续讨论", () =>
          openProjectWorkTextAction("继续讨论草稿", "draft-discuss", {
            project,
            draft_id: options.draft.id,
            revision: options.draft.revision,
          }),
        ),
        button(
          "保存新版本",
          projectWorkHandler(
            "revise",
            () => {
              if (!changed()) throw Error("任务内容没有变化");
              return {
                project,
                draft_id: options.draft.id,
                revision: options.draft.revision,
                ...projectWorkValues(controls),
              };
            },
            async () => {
              closeModal();
              await refreshProjectWork(project);
              notify("新版本已保存，请重新核对后确认。");
            },
          ),
        ),
        button(
          "确认此版本",
          projectWorkHandler(
            "confirm",
            () => {
              if (changed())
                throw Error("内容已修改，请先保存新版本并重新核对");
              return {
                project,
                draft_id: options.draft.id,
                revision: options.draft.revision,
              };
            },
            async () => {
              closeModal();
              await refreshProjectWork(project);
            },
          ),
          "primary",
        ),
      );
    } else
      actions.append(
        button(
          "创建",
          projectWorkHandler(
            "create",
            () => {
              const values = projectWorkValues(controls);
              if (!values.title || !values.instructions || !values.agent)
                throw Error("请填写标题、工作说明并选择负责人");
              return {
                project,
                ...values,
                ...(options.conversation_id &&
                (values.visibility === "group" ||
                  source.visibility === "private")
                  ? { conversation_id: options.conversation_id }
                  : {}),
                ...(options.source_message_id
                  ? { source_message_id: options.source_message_id }
                  : {}),
              };
            },
            async () => {
              closeModal();
              await refreshProjectWork(project);
            },
          ),
          "primary",
        ),
      );
  });
}
function openProjectWorkTextAction(title, action, target) {
  modal(title, (body, actions) => {
    const input = field(body, "内容", "");
    actions.append(
      button("取消", closeModal),
      button(
        "提交",
        projectWorkHandler(
          action,
          () => {
            if (!input.value.trim()) throw Error("请填写内容");
            return {
              ...target,
              body: input.value.trim(),
            };
          },
          async () => {
            closeModal();
            await refreshProjectWork(target.project);
          },
        ),
        "primary",
      ),
    );
  });
}

function openProjectWorkReference(project, work) {
  modal("引用项目消息", (body, actions) => {
    const input = field(body, "消息编号", "", null, "number");
    actions.append(
      button("取消", closeModal),
      button(
        "引用",
        projectWorkHandler(
          "reference",
          () => {
            const messageId = Number(input.value);
            if (!Number.isInteger(messageId)) throw Error("请输入消息编号");
            return {
              project,
              work_id: work.id,
              message_id: messageId,
            };
          },
          async () => {
            closeModal();
            await refreshProjectWork(project);
          },
        ),
        "primary",
      ),
    );
  });
}

function openProjectWorkScope(project, work) {
  modal("修改工作范围", (body, actions) => {
    const controls = {
      title: field(body, "标题", work.title || ""),
      instructions: field(body, "工作说明", work.instructions || ""),
      deliverable: field(body, "交付物", work.deliverable || ""),
      acceptance: field(body, "验收标准", work.acceptance || ""),
      validation: field(body, "验证命令", work.validation || ""),
      allowed_files: field(body, "允许修改的文件", work.allowed_files || ""),
    };
    const revision = work.revision;
    note(
      body,
      "当前版本 · " + revision + "。保存时会核对版本，避免覆盖其他修改。",
    );
    actions.append(
      button("取消", closeModal),
      button(
        "保存范围",
        projectWorkHandler(
          "update-scope",
          () => {
            const values = projectWorkValues(controls);
            if (!values.title || !values.instructions)
              throw Error("请填写标题和工作说明");
            return { project, work_id: work.id, revision, ...values };
          },
          async () => {
            closeModal();
            await refreshProjectWork(project);
          },
        ),
        "primary",
      ),
    );
  });
}

function openProjectWorkDetail(view, work) {
  const project = view.project.id;
  modal(work.title || "项目工作", (body, actions) => {
    const agent = (view.members || []).find(
      (item) => item.id === (work.agent || work.agent_id),
    );
    const status =
      {
        queued: "排队中",
        running: "执行中",
        waiting: "等待中",
        delivered: "已交付",
        accepted: "已验收",
        blocked: "受阻",
      }[work.status] ||
      work.status ||
      "未知";
    const revision = work.revision;
    body.append(
      el("div", "setting-row", "状态 · " + status),
      el("div", "setting-row", "版本 · " + (revision ?? "—")),
      el(
        "div",
        "setting-row",
        "负责人 · " + (agent?.name || work.agent || work.agent_id || ""),
      ),
      el("p", "", work.instructions || ""),
      el("div", "setting-row", "交付物 · " + (work.deliverable || "未填写")),
      el("div", "setting-row", "验收标准 · " + (work.acceptance || "未填写")),
      el("div", "setting-row", "验证 · " + (work.validation || "未填写")),
    );
    if (work.status_reason) body.append(el("p", "muted", work.status_reason));
    (work.deliveries || []).forEach((delivery) => {
      const card = el(
        "div",
        "task",
        "交付 · " + (delivery.summary || delivery.body || delivery.id || ""),
      );
      (delivery.links || []).forEach((value) => {
        const raw = typeof value === "string" ? value : value?.url;
        if (!raw) return;
        try {
          const url = new URL(raw);
          if (["http:", "https:"].includes(url.protocol)) {
            const link = el("a", "link small", value.label || url.href);
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            card.append(link);
            return;
          }
        } catch {}
        card.append(el("div", "small", raw));
      });
      if (delivery.message_id)
        appendProjectMessageReferences(
          card,
          view,
          [delivery.message_id],
          "交付消息",
        );
      body.append(card);
    });
    const referenceIds = [];
    (work.inputs || []).forEach((input) => {
      if (input.message_id) referenceIds.push(input.message_id);
      else
        body.append(
          el(
            "div",
            "task",
            "补充信息 · " + (input.body || input.kind || input.id || ""),
          ),
        );
    });
    appendProjectMessageReferences(body, view, referenceIds, "引用消息");
    (work.executions || []).forEach((execution) => {
      const row = el(
        "div",
        "task",
        "执行记录 · " +
          (execution.state || execution.status || execution.id || ""),
      );
      if (execution.error)
        row.append(el("div", "error small", execution.error));
      body.append(row);
    });
    const textAction = (label, action) =>
      button(label, () =>
        openProjectWorkTextAction(label + "项目工作", action, {
          project,
          work_id: work.id,
        }),
      );
    actions.append(button("关闭", closeModal));
    if (work.can_manage !== false && work.status !== "accepted")
      actions.append(
        button("修改范围", () => openProjectWorkScope(project, work)),
      );
    if (
      work.can_manage !== false &&
      ["queued", "running", "waiting", "blocked", "delivered"].includes(
        work.status,
      )
    )
      actions.append(textAction("继续", "follow-up"));
    if (work.can_manage !== false)
      actions.append(
        button("引用消息", () => openProjectWorkReference(project, work)),
      );
    if (work.can_manage !== false && work.status === "delivered")
      actions.append(
        button(
          "验收",
          projectWorkHandler(
            "accept",
            () => ({ project, work_id: work.id }),
            async () => {
              closeModal();
              await refreshProjectWork(project);
            },
          ),
          "primary",
        ),
      );
    if (
      work.can_manage !== false &&
      ["accepted", "delivered", "blocked"].includes(work.status)
    )
      actions.append(textAction("重新打开", "reopen"));
  });
}
async function selectConversation(conversation) {
  S.chatSelection = S.chatSelection || new Map();
  S.chatSelection.set(
    S.project,
    conversation.id || conversation.virtual_id || "group",
  );
  if (String(conversation.id || "").startsWith("new:")) {
    renderProject();
    return;
  }
  const view = await api("project", {
    id: S.project,
    visit: true,
    active_conversation_id: conversation.id,
  });
  if (S.project === view.project?.id) S.view = view;
  renderProject();
}

function groupMentionOptions(view) {
  const addressedWake = view.capabilities.includes("addressed_wake_v1");
  return [
    ["", addressedWake ? "普通聊天" : "默认由管理员回复"],
    ...view.members.map((agent) => [
      agent.id,
      (agent.id === view.project.admin_agent_id ? "交给管理员 · @" : "@") +
        (agent.name || agent.id),
    ]),
  ];
}

async function jumpToProjectMessage(project, message, conversation) {
  if (S.project !== project) return;
  if (S.chatSelection.get(project) !== conversation.id)
    await selectConversation(conversation);
  requestAnimationFrame(() => {
    if (
      S.project !== project ||
      S.chatSelection.get(project) !== conversation.id
    )
      return;
    const row = document.getElementById("project-message-" + message.id);
    row?.scrollIntoView({ block: "center" });
    row?.focus({ preventScroll: true });
  });
}

function appendProjectMessageReferences(container, view, ids, label) {
  [...new Set(ids || [])].forEach((id) => {
    const message = (view.messages || []).find((item) => item.id === id);
    const conversation = (view.conversations || []).find(
      (item) => message && item.id === message.conversation_id,
    );
    const text = (label || "关联消息") + " · #" + id;
    if (message && conversation)
      container.append(
        button(
          text,
          async () => {
            closeModal();
            await jumpToProjectMessage(view.project.id, message, conversation);
          },
          "link small",
        ),
      );
    else container.append(el("div", "muted small", text));
  });
}

function openProjectTaskTrace(view, task) {
  const trace = task.trace || {},
    rooms = view.conversations || [],
    source = (view.messages || []).find(
      (m) => m.id === trace.source_message_id,
    ),
    sourceRoom = rooms.find(
      (room) => source && room.id === source.conversation_id,
    ),
    deliveryRoom = rooms.find(
      (room) => room.id === trace.delivery_conversation_id,
    ),
    parent = (view.tasks || []).find(
      (item) => item.id === trace.parent_task_id,
    ),
    assignee = view.members.find((member) => member.id === task.agent_id),
    name = trace.agent_name || assignee?.name || task.agent_id;
  modal(task.title || "任务追溯", (body, actions) => {
    const id = field(body, "任务编号", task.id);
    id.readOnly = true;
    if (name) body.append(el("div", "setting-row", "负责人 · " + name));
    if (trace.node)
      body.append(
        el(
          "div",
          "setting-row",
          "执行设备 · " + (trace.node_name || trace.node),
        ),
      );
    if (trace.source_message_id)
      body.append(
        el("div", "setting-row", "来源消息 · #" + trace.source_message_id),
        ...(sourceRoom
          ? [
              button(
                "查看来源消息",
                async () => {
                  closeModal();
                  await jumpToProjectMessage(
                    view.project.id,
                    source,
                    sourceRoom,
                  );
                },
                "link small",
              ),
            ]
          : []),
      );
    if (parent)
      body.append(
        button(
          "查看上游任务",
          () => openProjectTaskTrace(view, parent),
          "link small",
        ),
      );
    if (deliveryRoom)
      body.append(
        button(
          "交付会话 · " + (deliveryRoom.title || "项目大群"),
          async () => {
            closeModal();
            await selectConversation(deliveryRoom);
          },
          "link small",
        ),
      );
    actions.append(
      button("关闭", closeModal),
      button(
        "复制任务编号",
        async () => {
          await copyText(task.id, id);
          notify("已复制任务编号");
        },
        "primary",
      ),
    );
  });
}

function confirmTaskCancellation(project, task) {
  modal("取消任务", (body, actions) => {
    body.append(el("p", "", task.title || task.id));
    note(body, "已交给设备的任务会在接收端确认后标记为已取消。");
    actions.append(
      button("返回", closeModal),
      button(
        "确认取消任务",
        async () => {
          await api("task-cancel", { project, task_id: task.id });
          closeModal();
          if (S.project === project) await refresh(true);
        },
        "danger",
      ),
    );
  });
}

function confirmLocalTaskStopped(project, task) {
  modal("核对本机任务", (body, actions) => {
    body.append(el("p", "", task.title || task.id));
    note(
      body,
      "请先在原执行电脑上检查任务进程及其子进程。确认都已停止后再继续。任务状态会在接收端回报后更新。",
    );
    actions.append(
      button("返回", closeModal),
      button(
        "确认原任务已停止",
        async () => {
          await api("task-resolve-local", {
            project,
            task_id: task.id,
            confirm_no_job: true,
          });
          closeModal();
          if (S.project === project) await refresh(true);
        },
        "danger",
      ),
    );
  });
}

function confirmPermissionRequest(project, agent, request, approve) {
  modal(approve ? "批准本次提权" : "拒绝本次提权", (body, actions) => {
    body.append(el("p", "", agent.name));
    if (request.reason) body.append(el("p", "", request.reason));
    note(
      body,
      approve
        ? "只有这项任务的下一次续跑可以访问工作区外。续跑结束后恢复原有权限。"
        : "这次申请会关闭，Agent 后续任务使用项目原有权限。",
    );
    actions.append(
      button("返回", closeModal),
      button(
        approve ? "确认批准" : "确认拒绝",
        async () => {
          await api("permission-decide", {
            project,
            request_id: request.id,
            approve,
          });
          closeModal();
          if (S.project === project) await refresh(true);
        },
        approve ? "primary" : "danger",
      ),
    );
  });
}

function startDirectConversation() {
  const v = S.view,
    me = S.dash.user.id,
    options = [
      ...v.members
        .filter((a) => a.owner_id === me)
        .map((a) => ({ id: a.id, name: a.name, kind: "human_agent" })),
      ...v.users
        .filter((u) => u.id !== me)
        .map((u) => ({
          id: u.id,
          name: u.username,
          kind: "human_human",
        })),
    ];
  modal("开始私聊", (body, actions) => {
    if (!options.length) {
      empty(body, "当前没有可以私聊的成员或 Agent。");
      actions.append(button("关闭", closeModal));
      return;
    }
    note(body, "你只能私聊自己的 Agent；人与人的私聊仅双方可见。");
    options.forEach((target) => {
      const existing = (v.conversations || []).find(
        (c) =>
          conversationKind(c) === target.kind &&
          conversationParticipants(c).includes(target.id),
      );
      const label =
        (target.kind === "human_agent" ? "Agent · " : "成员 · ") + target.name;
      body.append(
        button(
          label,
          () => {
            closeModal();
            selectConversation(
              existing || {
                id: "new:" + target.id,
                virtual_id: "new:" + target.id,
                kind: target.kind,
                title: target.name,
                recipient_id: target.id,
                participants: [me, target.id],
              },
            );
          },
          "conversation-start",
        ),
      );
    });
    actions.append(button("取消", closeModal));
  });
}

Object.assign(FoolshipDesktop.render, { renderProject });
Object.assign(FoolshipDesktop.events, {
  openProject,
  selectConversation,
  streamMessageText,
});
function renderProject() {
  const currentMessages = document.querySelector(".messages"),
    selection = window.getSelection?.();
  if (
    currentMessages &&
    selection?.rangeCount &&
    !selection.isCollapsed &&
    currentMessages.contains(selection.anchorNode)
  ) {
    S.dirty = true;
    return;
  }
  nav("project");
  const v = S.view,
    p = v.project,
    c = $("content"),
    me = S.dash.user.id,
    owner = p.owner_id === me;
  if (
    !Array.isArray(v.capabilities) ||
    !v.capabilities.includes("conversation_privacy_v2")
  ) {
    c.replaceChildren(
      head(p.name, "项目对话"),
      el(
        "div",
        "panel error",
        "协作服务版本过旧。请让服务所在电脑更新应用并重启团队服务，然后重新打开项目。为保护私聊，此版本不会显示旧服务返回的聊天内容。",
      ),
    );
    return;
  }
  const conversations = (v.conversations || []).slice();
  let group = conversations.find((x) => conversationKind(x) === "group");
  if (!group) {
    group = {
      id: "group",
      kind: "group",
      title: "项目大群",
      unread_count: 0,
    };
    conversations.unshift(group);
  }
  S.chatSelection = S.chatSelection || new Map();
  const saved = S.chatSelection.get(S.project) || group.id;
  let selected = conversations.find((x) => x.id === saved);
  if (!selected && saved.startsWith("new:")) {
    const target = saved.slice(4),
      agent = v.members.find((a) => a.id === target && a.owner_id === me),
      person = v.users.find((u) => u.id === target);
    if (agent || person)
      selected = {
        id: saved,
        kind: agent ? "human_agent" : "human_human",
        title: agent?.name || person?.username || target,
        recipient_id: target,
        participants: [me, target],
      };
  }
  if (!selected) selected = group;
  const selectedId = selected.id,
    kind = conversationKind(selected),
    isGroup = kind === "group",
    readOnly = kind === "agent_agent";
  const privateWorkAgent =
    kind === "human_agent"
      ? selected.recipient_id ||
        conversationParticipants(selected).find((id) => id !== me)
      : null;
  const createWorkOptions = {
    conversation_id:
      selectedId.startsWith("new:") || (!isGroup && !privateWorkAgent)
        ? undefined
        : selectedId,
    task: privateWorkAgent
      ? { visibility: "private", agent: privateWorkAgent }
      : {},
  };
  const oldBox =
    c.dataset.projectId === S.project && c.dataset.conversationId === selectedId
      ? c.querySelector(".messages")
      : null;
  const oldScroll = oldBox
    ? {
        top: oldBox.scrollTop,
        nearBottom:
          oldBox.scrollHeight - oldBox.scrollTop - oldBox.clientHeight < 36,
        outer: window.scrollY,
      }
    : null;
  const oldInternal = c.querySelector("#internalConversations"),
    internalOpen = oldInternal?.open || false;
  const oldTasks = c.querySelector("#projectTasks"),
    tasksOpen = oldTasks?.open || false;
  const oldMemory = c.querySelector("#projectMemory"),
    memoryOpen = oldMemory?.open || false;
  const oldMembers = c.querySelector("#projectMembers"),
    membersOpen = oldMembers?.open || false;
  const panelScroll = {
    tasks: oldTasks?.scrollTop || 0,
    memory: oldMemory?.scrollTop || 0,
    members: oldMembers?.scrollTop || 0,
  };
  c.dataset.projectId = S.project;
  c.dataset.conversationId = selectedId;
  c.replaceChildren(head(p.name, serviceName(v.server || p.server)));
  const layout = el("div", "conversation-layout");
  const rooms = el("aside", "panel conversation-rail");
  const roomHead = el("div", "row");
  roomHead.append(
    el("h3", "", "会话"),
    button("＋ 私聊", startDirectConversation, "link"),
  );
  rooms.append(roomHead);
  function roomButton(conversation) {
    const b = button(
      "",
      () => selectConversation(conversation),
      "conversation-item" + (conversation.id === selectedId ? " active" : ""),
    );
    const title = el(
      "span",
      "conversation-title",
      conversation.title || "未命名会话",
    );
    b.append(title);
    const count = Number(conversation.unread_count || 0);
    if (count > 0)
      b.append(el("span", "unread", String(count > 99 ? "99+" : count)));
    return b;
  }
  rooms.append(roomButton(group));
  const myChats = conversations.filter((x) =>
    ["human_agent", "human_human"].includes(conversationKind(x)),
  );
  rooms.append(el("div", "conversation-label", "我的私聊"));
  if (myChats.length) myChats.forEach((x) => rooms.append(roomButton(x)));
  else
    rooms.append(
      el("div", "muted small conversation-hint", "点“＋ 私聊”开始对话"),
    );
  const internal = el("details", "internal-conversations");
  internal.id = "internalConversations";
  internal.open = internalOpen;
  const internals = conversations.filter(
    (x) => conversationKind(x) === "agent_agent",
  );
  const unread = internals.reduce(
    (sum, x) => sum + Number(x.unread_count || 0),
    0,
  );
  const summary = el("summary", "", "Agent 内部会话");
  if (unread > 0)
    summary.append(el("span", "unread", String(unread > 99 ? "99+" : unread)));
  internal.append(summary);
  if (internals.length)
    internals.forEach((x) => internal.append(roomButton(x)));
  else
    internal.append(el("div", "muted small conversation-hint", "暂无内部会话"));
  rooms.append(internal);
  const chat = el("section", "panel chat");
  const chatHead = el("div", "chat-head");
  chatHead.append(el("strong", "", selected.title || "项目大群"));
  const privacy = {
    group: "所有项目成员可见",
    human_agent: "仅你和该 Agent 可见",
    human_human: "仅对话双方可见",
    agent_agent: "项目成员可旁观，其他 Agent 不可见",
  };
  chatHead.append(el("span", "muted small", privacy[kind] || ""));
  chat.append(chatHead);
  const msgs = el("div", "messages");
  const visible = (v.messages || []).filter(
    (m) =>
      m.conversation_id === selectedId ||
      (!m.conversation_id && isGroup && !m.recipient_id),
  );
  const streamIds = new Set(visible.map((m) => S.project + ":" + m.id));
  for (const key of S.streamTexts.keys())
    if (!streamIds.has(key)) S.streamTexts.delete(key);
  if (!visible.length)
    empty(msgs, isGroup ? "在项目大群开始讨论。" : "这段会话还没有消息。");
  const tasksById = new Map((v.tasks || []).map((task) => [task.id, task]));
  const workByMessage = new Map();
  (v.work?.drafts || [])
    .filter((draft) => draft.status === "draft")
    .forEach((draft) =>
      (draft.reference_message_ids || []).forEach((id) => {
        const records = workByMessage.get(id) || [];
        records.push({ kind: "draft", record: draft });
        workByMessage.set(id, records);
      }),
    );
  (v.work?.items || []).forEach((work) =>
    (work.inputs || []).forEach((input) => {
      if (!input.message_id) return;
      const records = workByMessage.get(input.message_id) || [];
      records.push({ kind: "work", record: work });
      workByMessage.set(input.message_id, records);
    }),
  );
  visible.forEach((m) => {
    const baseSender = m.sender_name || m.sender_username || m.sender_id || "?",
      clientLabel = { codex: "Codex", dsh: "DSH" }[m.sender_client],
      sender = baseSender + (clientLabel ? " · " + clientLabel : ""),
      row = el("div", "message"),
      bubble = el("div", "bubble"),
      name = el("div", "name", sender);
    row.id = "project-message-" + m.id;
    row.tabIndex = -1;
    if (m.mention_agent_name)
      name.append(el("span", "channel", "@" + m.mention_agent_name));
    const body = el("div", "message-text");
    FoolshipRender.render(body, m.body);
    streamMessageText(m, body, msgs);
    bubble.append(name, body, el("div", "muted small", date(m.created_at)));
    const linkedTask = tasksById.get(m.task_id);
    if (linkedTask)
      bubble.append(
        button(
          "查看关联任务",
          () => openProjectTaskTrace(v, linkedTask),
          "link small",
        ),
      );
    (m.attachments || []).forEach((file) =>
      bubble.append(attachmentCard(file, S.project)),
    );
    if (
      (isGroup || privateWorkAgent) &&
      (v.capabilities || []).includes("project_work_v1")
    )
      bubble.append(
        button(
          "创建工作",
          () =>
            openProjectWorkForm(v, {
              ...createWorkOptions,
              source_message_id: m.id,
              task: { ...createWorkOptions.task, instructions: m.body },
            }),
          "link small",
        ),
      );
    (workByMessage.get(m.id) || []).forEach(({ kind, record }) =>
      bubble.append(
        button(
          kind === "draft" ? "查看工作草稿" : "查看项目工作",
          () =>
            kind === "draft"
              ? openProjectWorkForm(v, { draft: record })
              : openProjectWorkDetail(v, record),
          "link small",
        ),
      ),
    );
    const request = m.permission_request;
    if (request?.id) {
      const agent = v.members.find((item) => item.id === request.agent_id);
      if (agent?.owner_id === me) {
        const state = {
          pending: "等待机器主人批准",
          approved: "已批准一次续跑",
          consumed: "单次授权已使用",
          denied: "申请已拒绝",
        }[request.state];
        if (state) bubble.append(el("div", "muted small", state));
        if (request.state === "pending") {
          bubble.append(
            button(
              "批准本次",
              () => confirmPermissionRequest(p.id, agent, request, true),
              "link small",
            ),
            button(
              "拒绝",
              () => confirmPermissionRequest(p.id, agent, request, false),
              "link small",
            ),
          );
        }
      }
    }
    row.append(el("span", "avatar", sender.slice(0, 1)), bubble);
    msgs.append(row);
  });
  chat.append(msgs);
  if (readOnly) {
    chat.append(
      el("div", "composer-readonly", "Agent 内部会话仅供项目成员旁观。"),
    );
  } else {
    const composer = el("form", "composer"),
      draftKey = S.project + ":" + selectedId;
    const messageId = () =>
      globalThis.crypto?.randomUUID?.() ||
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = Math.floor(Math.random() * 16);
        return (c === "x" ? r : (r & 3) | 8).toString(16);
      });
    const draft = S.drafts.get(draftKey) || {
      body: "",
      mention: "",
      client_message_id: messageId(),
    };
    if (!draft.client_message_id) draft.client_message_id = messageId();
    if (!S.drafts.has(draftKey)) S.drafts.set(draftKey, draft);
    draft.attachments ||= [];
    const projectId = S.project;
    const mention = el("select", "mention-agent"),
      text = el("textarea");
    if (isGroup) {
      mention.setAttribute("aria-label", "群消息发送方式");
      groupMentionOptions(v).forEach(([value, label]) => {
        const option = el("option", "", label);
        option.value = value;
        mention.append(option);
      });
      mention.value = draft.mention || "";
      composer.append(mention);
    }
    text.value = draft.body;
    text.rows = 2;
    text.placeholder = isGroup
      ? "发送到项目大群，Enter 发送 · Shift+Enter 换行 · 可拖入或粘贴图片"
      : "发送私聊消息，Enter 发送 · Shift+Enter 换行";
    const save = () => {
      const mentionValue = isGroup ? mention.value : "";
      const previous = S.drafts.get(draftKey);
      S.drafts.set(draftKey, {
        ...previous,
        body: text.value,
        mention: mentionValue,
        client_message_id:
          previous &&
          previous.body === text.value &&
          previous.mention === mentionValue
            ? previous.client_message_id
            : messageId(),
      });
    };
    text.oninput = () => {
      save();
      // Grow with the text, up to six lines, then scroll.
      text.style.height = "auto";
      text.style.height = Math.min(text.scrollHeight, 168) + "px";
    };
    if (isGroup) mention.onchange = save;
    // Enter sends and Shift+Enter breaks the line; a visible send button is noise.
    const send = button("", null, "submit-hidden");
    send.type = "submit";
    send.tabIndex = -1;
    send.setAttribute("aria-hidden", "true");
    composer.append(text, send);
    if ((v.capabilities || []).includes("chat_attachments_v1")) {
      const picker = el("input"),
        files = el("div", "attachment-draft");
      picker.type = "file";
      picker.multiple = true;
      picker.hidden = true;
      const redrawFiles = () => {
        files.replaceChildren();
        (S.drafts.get(draftKey)?.attachments || []).forEach((file) => {
          const row = el(
            "div",
            "small",
            "📎 " + file.name + " · " + fileSize(file.size),
          );
          row.append(
            button("移除", () => {
              const current = S.drafts.get(draftKey);
              current.attachments = current.attachments.filter(
                (f) => f.id !== file.id,
              );
              current.client_message_id = messageId();
              redrawFiles();
            }),
          );
          files.append(row);
        });
      };
      const addFiles = async (incoming) => {
        const current = S.drafts.get(draftKey);
        if (!current || sending || current.uploading)
          throw new Error("请等待当前文件处理完成");
        if (current.attachments.length + incoming.length > 5)
          throw new Error("每条消息最多 5 个附件");
        if (incoming.some((file) => file.size > 5 * 1024 * 1024))
          throw new Error("每个附件最大 5 MB");
        current.uploading = true;
        files.append(el("span", "muted small", "正在上传附件…"));
        try {
          for (const file of incoming) {
            const encoded = await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () =>
                resolve(String(reader.result).split(",")[1]);
              reader.onerror = () =>
                reject(new Error("无法读取：" + file.name));
              reader.readAsDataURL(file);
            });
            const metadata = await api("attachment-upload", {
              project: projectId,
              name: file.name || "截图.png",
              base64: encoded,
            });
            (S.drafts.get(draftKey) || current).attachments.push(metadata);
            (S.drafts.get(draftKey) || current).client_message_id = messageId();
          }
        } finally {
          (S.drafts.get(draftKey) || current).uploading = false;
          redrawFiles();
        }
      };
      picker.onchange = () =>
        run(async () => {
          await addFiles(Array.from(picker.files));
          picker.value = "";
        });
      text.addEventListener("paste", (e) => {
        const incoming = Array.from(e.clipboardData?.files || []);
        if (incoming.length) {
          e.preventDefault();
          run(() => addFiles(incoming));
        }
      });
      composer.ondragover = (e) => {
        e.preventDefault();
      };
      composer.ondrop = (e) => {
        e.preventDefault();
        const incoming = Array.from(e.dataTransfer?.files || []);
        if (incoming.length) run(() => addFiles(incoming));
      };
      // Attachments arrive by drag and drop or paste; no button competes with the
      // text field.
      composer.append(picker, files);
      redrawFiles();
    } else {
      composer.append(
        el(
          "div",
          "muted small",
          "请将协作服务升级到当前版本并重启后再发送文件和截图。",
        ),
      );
    }
    let sending = false,
      composing = false;
    text.addEventListener("compositionstart", () => {
      composing = true;
    });
    text.addEventListener("compositionend", () => {
      composing = false;
    });
    text.onkeydown = (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing || composing) return;
      e.preventDefault();
      composer.requestSubmit(send);
    };
    composer.onsubmit = (e) => {
      e.preventDefault();
      run(async () => {
        if (sending) return;
        const submission = S.drafts.get(draftKey) || draft;
        if (submission.uploading) throw new Error("附件仍在上传，请稍后发送");
        const attached = submission.attachments || [];
        const body = text.value.trim() || (attached.length ? "📎 附件" : "");
        if (!body) return;
        sending = true;
        const recipient =
          selected.recipient_id ||
          (!isGroup
            ? conversationParticipants(selected).find((id) => id !== me)
            : null);
        try {
          await api("message", {
            project: S.project,
            body,
            conversation_id: selectedId.startsWith("new:") ? null : selectedId,
            recipient_id: recipient || null,
            mention_agent_id: isGroup ? mention.value || null : null,
            client_message_id: submission.client_message_id,
            attachment_ids: attached.map((file) => file.id),
          });
          text.value = "";
          S.drafts.delete(draftKey);
          if (selectedId.startsWith("new:")) {
            const view = await api("project", {
              id: S.project,
              visit: true,
            });
            S.view = view;
            const created = (view.conversations || []).find(
              (room) =>
                conversationKind(room) === kind &&
                conversationParticipants(room).includes(recipient),
            );
            if (created) S.chatSelection.set(S.project, created.id);
            renderProject();
          } else {
            // One project read instead of a full dashboard fan-out: the message
            // must appear as soon as the service has it, not a second later.
            S.view = await api("project", {
              id: S.project,
              visit: false,
              active_conversation_id: selectedId,
            });
            renderProject();
            refresh().catch(() => {});
          }
        } finally {
          sending = false;
        }
      }, send);
    };
    chat.append(composer);
  }
  const side = el("aside", "project-side");
  if ((v.capabilities || []).includes("project_work_v1")) {
    const projectWork = el("details", "panel side-section"),
      drafts = (v.work?.drafts || []).filter(
        (draft) => draft.status === "draft",
      ),
      items = v.work?.items || [];
    projectWork.open = true;
    projectWork.append(
      el("summary", "", "项目工作 · " + (drafts.length + items.length)),
      button(
        "＋ 创建工作",
        () => openProjectWorkForm(v, createWorkOptions),
        "link small",
      ),
    );
    drafts
      .filter(
        (draft) =>
          !draft.conversation_id || draft.conversation_id === selectedId,
      )
      .forEach((draft) => {
        const card = el(
          "div",
          "task",
          (draft.title || "未命名草稿") + " · 草稿 v" + draft.revision,
        );
        card.append(
          button(
            "审阅并确认",
            () => openProjectWorkForm(v, { draft }),
            "link small",
          ),
        );
        projectWork.append(card);
      });
    items
      .filter(
        (item) => !item.conversation_id || item.conversation_id === selectedId,
      )
      .forEach((item) => {
        const card = el(
          "div",
          "task",
          (item.title || item.id) + " · " + item.status,
        );
        card.append(
          button(
            "查看详情",
            () => openProjectWorkDetail(v, item),
            "link small",
          ),
        );
        projectWork.append(card);
      });
    side.append(projectWork);
  }
  const tasks = el("details", "panel side-section");
  tasks.id = "projectTasks";
  tasks.open = tasksOpen;
  tasks.append(el("summary", "", "任务与执行 · " + (v.tasks?.length || 0)));
  (v.tasks || []).forEach((t) => {
    const who =
      v.members.find((a) => a.id === t.agent_id)?.name || t.agent_id || "Agent";
    const issue = String(t.error || "").includes("No working Codex executable")
      ? " · 目标机器未找到 Codex CLI"
      : ["queued", "starting", "running"].includes(t.state) &&
          v.members.some((a) => a.id === t.agent_id && !a.online)
        ? " · 等待机器上线"
        : "";
    const active = ["queued", "starting", "running", "unknown"].includes(
      t.state,
    );
    const status =
      t.cancel_requested && active
        ? "等待接收端取消"
        : t.display_status ||
          (t.state === "unknown" ? "待核对" : t.state || t.status || "");
    const task = el("div", "task");
    task.append(
      el(
        "div",
        "",
        who + " · " + (t.title || t.body || t.id) + " · " + status + issue,
      ),
      button("任务追溯", () => openProjectTaskTrace(v, t), "link small"),
    );
    if (t.can_cancel && !t.cancel_requested) {
      task.append(
        button(
          "取消任务",
          () => confirmTaskCancellation(p.id, t),
          "link small",
        ),
      );
    }
    if (
      t.state === "unknown" &&
      t.local_recovery &&
      (S.dash.devices || []).some(
        (d) => d.local && (d.node || d.id) === t.local_recovery.node,
      )
    ) {
      task.append(
        button(
          "核对本机任务",
          () => confirmLocalTaskStopped(p.id, t),
          "link small",
        ),
      );
    }
    tasks.append(task);
  });
  (v.executions || []).forEach((e) =>
    tasks.append(
      el(
        "div",
        "task",
        (e.job_id || e.id) + " · " + (e.state || e.status || ""),
      ),
    ),
  );
  side.append(tasks);
  const memory = el("details", "panel side-section");
  memory.id = "projectMemory";
  memory.open = memoryOpen;
  memory.append(
    el("summary", "", "项目记忆 · " + (v.project_memory?.notes?.length || 0)),
  );
  note(
    memory,
    "这里记录整个项目共享的目标、决策与进展。私聊内容不会自动进入项目记忆。",
  );
  (v.project_memory?.notes || []).forEach((item) => {
    const row = el("div", "memory-note"),
      author =
        v.users.find((u) => u.id === item.author_id)?.username ||
        item.author_id ||
        "成员";
    row.append(
      el("div", "small muted", author + " · " + date(item.created_at)),
      (() => {
        const report = el("div", "message-text");
        FoolshipRender.render(report, item.body);
        return report;
      })(),
    );
    if (owner || item.author_id === me)
      row.append(
        button(
          "删除",
          async () => {
            await api("memory-note-delete", {
              project: S.project,
              id: item.id,
            });
            await refresh(true);
          },
          "link small",
        ),
      );
    memory.append(row);
  });
  const memoryForm = el("form", "memory-form"),
    memoryText = el("textarea");
  memoryText.rows = 2;
  memoryText.placeholder = "记下已确认的目标、决策或重要结果…";
  const addMemory = button("加入项目记忆", null, "primary");
  addMemory.type = "submit";
  memoryForm.append(memoryText, addMemory);
  memoryForm.onsubmit = (e) => {
    e.preventDefault();
    run(async () => {
      if (!memoryText.value.trim()) return;
      await api("memory-note-add", {
        project: S.project,
        body: memoryText.value.trim(),
      });
      await refresh(true);
    }, addMemory);
  };
  memory.append(memoryForm);
  side.append(memory);
  const members = el("details", "panel side-section");
  members.id = "projectMembers";
  members.open = membersOpen;
  members.append(el("summary", "", "成员与项目 Agent"));
  v.users.forEach((u) =>
    members.append(
      el("div", "member", u.username + (u.id === p.owner_id ? " · 群主" : "")),
    ),
  );
  const agentCommands = v.project_agent_commands || [];
  for (const command of agentCommands) {
    if (command.action === "project-agent-create") {
      members.append(
        el(
          "div",
          command.state === "failed" ? "member" : "member pending",
          (command.name || "Agent") +
            " · " +
            (command.state === "failed"
              ? command.error || "创建失败"
              : command.state === "applied"
                ? "已创建，等待设备同步"
                : "等待目标设备创建"),
        ),
      );
    }
  }
  v.members.forEach((a) => {
    const row = el("div", "member");
    row.append(
      el(
        "span",
        "",
        a.name +
          (a.id === p.admin_agent_id ? " · 管理员" : "") +
          (a.online ? "" : " · 机器离线"),
      ),
    );
    if (a.can_manage_project_agent) {
      const command = agentCommands.find(
        (item) => item.backend_project === a.backend_project,
      );
      const pending = command && ["queued", "claimed"].includes(command.state);
      const configure = button(
        "设置",
        () => openProjectAgentForm(a),
        "link small",
      );
      const remove = button(
        "移除",
        () => removeProjectAgent(p.id, a),
        "link small",
      );
      configure.disabled = remove.disabled = !!pending;
      row.append(
        el(
          "span",
          "muted small",
          pending ? "等待目标设备确认" : a.enabled ? "已启用" : "已暂停",
        ),
        configure,
        remove,
      );
      if (command?.state === "failed")
        row.append(el("span", "muted small", command.error || "操作失败"));
    }
    members.append(row);
  });
  members.append(button("新增项目 Agent", () => openProjectAgentForm()));
  if (Array.isArray(v.mcp_approval_kinds))
    members.append(
      button("MCP 审批权限", () =>
        editMcpApprovalPolicy(p.id, v.mcp_approval_kinds),
      ),
    );
  if (owner) {
    members.append(button("邀请成员", invite));
    const admin = field(members, "管理员 Agent", p.admin_agent_id || "", [
      ["", "未设置"],
      ...v.members.map((a) => [a.id, a.name]),
    ]);
    members.append(
      button("保存管理员", async () => {
        await api("set-admin", {
          project: S.project,
          agent: admin.value,
        });
        await refresh(true);
      }),
    );
  }
  side.append(members);
  const work = el("div", "chat-column");
  work.append(chat, side);
  layout.append(rooms, work);
  c.append(layout);
  if (!oldScroll) msgs.scrollTop = msgs.scrollHeight;
  else
    requestAnimationFrame(() => {
      if (
        c.dataset.projectId !== S.project ||
        c.dataset.conversationId !== selectedId
      )
        return;
      msgs.scrollTop = oldScroll.nearBottom
        ? msgs.scrollHeight
        : Math.min(
            oldScroll.top,
            Math.max(0, msgs.scrollHeight - msgs.clientHeight),
          );
      window.scrollTo({ top: oldScroll.outer, behavior: "instant" });
    });
  requestAnimationFrame(() => {
    if (
      c.dataset.projectId !== S.project ||
      c.dataset.conversationId !== selectedId
    )
      return;
    tasks.scrollTop = panelScroll.tasks;
    memory.scrollTop = panelScroll.memory;
    members.scrollTop = panelScroll.members;
  });
}
function editMcpApprovalPolicy(project, current) {
  modal("MCP 审批权限", (body, actions) => {
    note(
      body,
      "代言者可以查看待审批。勾选后，它也可以代你批准或拒绝本项目中你自己设备的对应申请。",
    );
    const choices = [
      ["permission", "额外执行权限（一次续跑）"],
      ["attachment", "发送附件"],
      ["execution", "执行已登记的命令"],
    ].map(([kind, label]) => {
      const row = el("label", "setting-row", label);
      const check = el("input", "check");
      check.type = "checkbox";
      check.checked = current.includes(kind);
      row.append(check);
      body.append(row);
      return [kind, check];
    });
    actions.append(
      button("返回", closeModal),
      button(
        "保存审批权限",
        async () => {
          const kinds = choices
            .filter(([, check]) => check.checked)
            .map(([kind]) => kind);
          await api("mcp-approval-policy", { project, kinds });
          closeModal();
          if (S.project === project) await refresh(true);
        },
        "primary",
      ),
    );
  });
}
function invite() {
  modal("邀请成员", (b, a) => {
    note(b, "对方加入后可在项目内创建自己的 Agent。");
    a.append(
      button("关闭", closeModal),
      button(
        "生成邀请",
        async () => {
          const r = await api("invite", { project: S.project });
          b.replaceChildren();
          const code = field(b, "项目邀请代码", r.invite);
          code.readOnly = true;
          note(b, "协作服务：" + r.server + "。请把地址和邀请代码交给对方。");
          a.replaceChildren(button("完成", closeModal, "primary"));
        },
        "primary",
      ),
    );
  });
}
function removeProjectAgent(project, agent) {
  modal("移除项目 Agent", (body, actions) => {
    body.append(el("p", "", agent.name));
    note(
      body,
      "Agent 将停止在此项目接收新任务。工作文件、会话和任务历史会保留。请先结束正在执行的任务。",
    );
    actions.append(
      button("取消", closeModal),
      button(
        "确认移除",
        async () => {
          await api("project-agent-delete", { project, agent_id: agent.id });
          closeModal();
          notify("移除请求已提交。");
          if (S.project === project) await refresh(true);
        },
        "danger",
      ),
    );
  });
}

function projectWorkspaceReady(view, node, user) {
  return (
    (view.workspaces || []).some(
      (item) => item.node === node && item.status === "ready",
    ) ||
    view.members.some((item) => item.node === node && item.owner_id === user)
  );
}

async function openProjectAgentForm(agent = null) {
  const view = S.view;
  const project = view.project.id;
  const server = view.server || view.project.server || S.server;
  const user = S.dash.user.id;
  const devices = (S.dash.devices || []).filter(
    (device) =>
      (device.server || S.server) === server &&
      (agent ? (device.node || device.id) === agent.node : !!device.last_seen),
  );
  const localConfig = devices.some((device) => device.local)
    ? await api("settings-get").catch(() => null)
    : null;
  if (S.project !== project) return;
  const models = localConfig?.executor?.models || [];
  const current = agent?.project_agent_settings || {};

  modal(agent ? "项目 Agent 设置" : "新增项目 Agent", (body, actions) => {
    actions.append(button("取消", closeModal));
    if (!agent && !devices.length) {
      note(body, "请先将一台自己的机器接入该项目所在的协作服务。");
      return;
    }
    const name = field(body, "Agent 名称", agent?.name || "");
    name.maxLength = 120;
    let machine;
    if (agent) {
      note(body, "执行机器 · " + deviceName(agent.node, server));
    } else {
      machine = field(body, "执行机器", "", [
        ["", "选择已接入的机器"],
        ...devices.map((device) => [
          device.node || device.id,
          device.name +
            (device.local ? " · 本机" : "") +
            (device.online ? "" : " · 离线"),
        ]),
      ]);
    }
    const selectedNode = () => agent?.node || machine.value;
    const selectedDevice = () =>
      devices.find((device) => (device.node || device.id) === selectedNode());
    const workspace = el("div");
    const modelFields = el("div");
    body.append(workspace, modelFields);
    let model;
    const effort = field(
      body,
      "思考强度",
      current.effort || "medium",
      ["low", "medium", "high", "xhigh", "max", "ultra"].map((value) => [
        value,
        value,
      ]),
    );
    const enabled = field(
      body,
      "接收项目任务",
      String(current.enabled ?? true),
      [
        ["true", "启用"],
        ["false", "暂停"],
      ],
    );
    const stageBox = el("div");
    stageBox.append(el("span", "label", "允许的工作阶段"));
    const stages = [
      ["discuss", "讨论"],
      ["review", "审阅"],
      ["implement", "实现"],
    ].map(([value, label]) => {
      const row = el("label", "setting-row", label);
      const check = el("input", "check");
      check.type = "checkbox";
      check.checked = (current.stages || ["discuss", "review"]).includes(value);
      row.append(check);
      stageBox.append(row);
      return [value, check];
    });
    body.append(stageBox);
    const timeout = field(
      body,
      "每轮超时（秒）",
      current.timeout || 600,
      null,
      "number",
    );
    timeout.min = "30";
    timeout.max = "86400";
    const strict = field(
      body,
      "实现任务安全模式",
      String(!!current.strict_worktree),
      [
        ["false", "关闭"],
        ["true", "仅允许 linked worktree 且限制变更文件"],
      ],
    );
    const attachment = field(
      body,
      "附件发送权限",
      current.attachment_policy || "ask",
      [
        ["ask", "请求批准"],
        ["auto", "帮我批准"],
        ["full", "直接发送"],
      ],
    );
    const network = field(
      body,
      "模型请求代理",
      current.network?.mode || "machine",
      [
        ["machine", "跟随执行机器设置"],
        ["direct", "直连（不走代理）"],
        ["custom", "自定义端口"],
      ],
    );
    const port = field(
      body,
      "代理端口",
      current.network?.port || "",
      null,
      "number",
    );
    port.min = "1";
    port.max = "65535";
    const syncPort = () => {
      port.disabled = network.value !== "custom";
    };
    network.onchange = syncPort;
    syncPort();

    const workspaceStates = new Map();
    const submit = button(
      agent ? "保存设置" : "创建 Agent",
      async () => {
        const node = selectedNode();
        const fields = {
          project,
          name: name.value.trim(),
          model: model?.value.trim(),
          effort: effort.value,
          enabled: enabled.value === "true",
          stages: stages
            .filter(([, check]) => check.checked)
            .map(([value]) => value),
          timeout: Number(timeout.value),
          strict_worktree: strict.value === "true",
          attachment_policy: attachment.value,
          network: { mode: network.value },
        };
        if (!node || !fields.name || !fields.model)
          throw Error("请选择执行机器并填写名称和模型");
        if (!agent && workspaceStates.get(node) !== "ready")
          throw Error("请先设置这个项目在目标机器上的工作目录");
        if (!fields.stages.length) throw Error("请至少选择一个工作阶段");
        if (
          !Number.isInteger(fields.timeout) ||
          fields.timeout < 30 ||
          fields.timeout > 86400
        )
          throw Error("每轮超时必须为 30 至 86400 秒");
        if (network.value === "custom") {
          const value = Number(port.value);
          if (!Number.isInteger(value) || value < 1 || value > 65535)
            throw Error("代理端口必须在 1 到 65535 之间");
          fields.network.port = value;
        }
        if (agent)
          await api("project-agent-configure", {
            ...fields,
            agent_id: agent.id,
          });
        else await api("project-agent-create", { ...fields, node });
        closeModal();
        notify(
          agent ? "设置请求已提交。" : "创建请求已提交，等待目标设备确认。",
        );
        if (S.project === project) await refresh(true);
      },
      "primary",
    );
    actions.append(submit);

    function renderWorkspace() {
      workspace.replaceChildren();
      const node = selectedNode();
      if (agent || !node) {
        submit.disabled = !node;
        return;
      }
      if (!workspaceStates.has(node))
        workspaceStates.set(
          node,
          projectWorkspaceReady(view, node, user) ? "ready" : "unset",
        );
      const state = workspaceStates.get(node);
      submit.disabled = state !== "ready";
      if (state === "ready") {
        workspace.append(el("div", "muted small", "项目工作目录已设置"));
        return;
      }
      const row = el("div", "note workspace-row");
      row.append(
        el(
          "span",
          "",
          state === "pending"
            ? "目标设备正在配置项目工作目录"
            : "请先设置项目工作目录",
        ),
      );
      if (state === "pending") {
        row.append(
          button("检查配置状态", async () => {
            const latest = await api("project", { id: project });
            if (projectWorkspaceReady(latest, node, user))
              workspaceStates.set(node, "ready");
            renderWorkspace();
          }),
        );
      } else {
        row.append(
          button(
            selectedDevice()?.local ? "选择目录" : "使用默认目录",
            async () => {
              const result = await api("workspace-configure", {
                project,
                node,
                server,
              });
              if (result.pending) workspaceStates.set(node, "pending");
              else if (result.configured) workspaceStates.set(node, "ready");
              renderWorkspace();
            },
          ),
        );
      }
      workspace.append(row);
    }

    function renderMachine() {
      renderWorkspace();
      modelFields.replaceChildren();
      const node = selectedNode();
      if (!node) {
        model = null;
        return;
      }
      const saved = current.model || agent?.model || "";
      if (selectedDevice()?.local && (models.length || saved)) {
        const choices = models.map(({ name, executor }) => [
          name,
          name +
            " · " +
            (executor === "dsh" ? "DeepSeek Harness" : "Codex CLI"),
        ]);
        if (saved && !choices.some(([name]) => name === saved))
          choices.push([saved, saved + " · 当前配置"]);
        model = field(modelFields, "模型", saved, [
          ["", "选择模型"],
          ...choices,
        ]);
      } else {
        model = field(modelFields, "模型 ID", saved);
        note(
          modelFields,
          selectedDevice()?.local
            ? "可在设置的本机可用模型列表中查看模型 ID。"
            : "填写目标机器的可用模型列表中的模型 ID。",
        );
      }
    }

    if (machine) machine.onchange = renderMachine;
    renderMachine();
  });
}
