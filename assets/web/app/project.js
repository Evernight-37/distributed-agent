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

Object.assign(HarnessDesktop.render, { renderProject });
Object.assign(HarnessDesktop.events, {
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
  visible.forEach((m) => {
    const baseSender = m.sender_name || m.sender_username || m.sender_id || "?",
      sender = baseSender + (m.sender_client === "codex" ? " · Codex" : ""),
      row = el("div", "message"),
      bubble = el("div", "bubble"),
      name = el("div", "name", sender);
    if (m.mention_agent_name)
      name.append(el("span", "channel", "@" + m.mention_agent_name));
    const body = el("p", "", m.body);
    streamMessageText(m, body, msgs);
    bubble.append(name, body, el("div", "muted small", date(m.created_at)));
    (m.attachments || []).forEach((file) =>
      bubble.append(attachmentCard(file, S.project)),
    );
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
      mention.setAttribute("aria-label", "呼叫 Agent");
      const defaultMention = el("option", "", "默认由管理员回复");
      defaultMention.value = "";
      mention.append(defaultMention);
      v.members.forEach((a) => {
        const option = el("option", "", "@" + (a.name || a.id));
        option.value = a.id;
        mention.append(option);
      });
      mention.value = draft.mention || "";
      composer.append(mention);
    }
    text.value = draft.body;
    text.rows = 2;
    text.placeholder = isGroup
      ? "发送到项目大群；可选择呼叫指定 Agent…"
      : "发送私聊消息…";
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
    text.oninput = save;
    if (isGroup) mention.onchange = save;
    const send = button("发送", null, "primary");
    send.type = "submit";
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
      composer.append(
        button("添加文件 / 图片", () => picker.click()),
        picker,
        files,
        el(
          "div",
          "muted small",
          "可拖入文件或粘贴截图；每个最大 5 MB，每条最多 5 个。",
        ),
      );
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
          } else await refresh(true);
        } finally {
          sending = false;
        }
      }, send);
    };
    chat.append(composer);
  }
  const side = el("aside", "project-side");
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
    tasks.append(
      el(
        "div",
        "task",
        who +
          " · " +
          (t.title || t.body || t.id) +
          " · " +
          (t.display_status || t.state || t.status || "") +
          issue,
      ),
    );
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
      el("p", "", item.body),
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
  members.append(el("summary", "", "成员与员工"));
  v.users.forEach((u) =>
    members.append(
      el("div", "member", u.username + (u.id === p.owner_id ? " · 群主" : "")),
    ),
  );
  v.members.forEach((a) =>
    members.append(
      el(
        "div",
        "member",
        a.name +
          (a.id === p.admin_agent_id ? " · 管理员" : "") +
          (a.online ? "" : " · 机器离线"),
      ),
    ),
  );
  members.append(button("派遣我的员工", dispatch));
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
function invite() {
  modal("邀请成员", (b, a) => {
    note(b, "邀请将授予对方项目成员权限，对方可派遣自己的员工。");
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
function dispatch() {
  modal("派遣我的员工", (b, a) => {
    const server = S.view.server || S.view.project.server || S.server;
    note(
      b,
      "这里只显示已接入 " +
        serviceName(server) +
        " 的员工。工作目录按“项目 × 机器”设置；同一机器派到本项目的员工共享该目录，但各自保留独立会话。",
    );
    const checks = [];
    const eligible = S.dash.employees.filter(
      (e) => employeeServer(e) === server,
    );
    if (!eligible.length)
      note(
        b,
        "该服务还没有你的员工。请先在设置中将机器接入该服务，再添加员工。",
      );
    const workspaceState = new Map(
      (S.view.workspaces || []).map((w) => [w.node, w.status === "ready"]),
    );
    (S.view.members || [])
      .filter((member) => member.owner_id === S.dash.user.id)
      .forEach((member) => workspaceState.set(member.node, true));
    [...new Set(eligible.map((e) => e.node))].forEach((node) => {
      const local = (S.dash.devices || []).some(
          (d) => d.local && (d.node || d.id) === node,
        ),
        block = el("div", "note workspace-row"),
        status = el(
          "span",
          workspaceState.get(node) ? "success small" : "pending small",
          workspaceState.get(node) ? "已配置" : "尚未配置",
        );
      block.prepend(
        el("strong", "", deviceName(node, server) + " · 项目工作目录 "),
        status,
      );
      if (local)
        block.append(
          button(
            workspaceState.get(node) ? "更换目录" : "选择目录",
            async () => {
              const result = await api("workspace-configure", {
                project: S.project,
                node,
                server,
              });
              if (!result.configured) return;
              workspaceState.set(node, true);
              status.className = "success small";
              status.textContent = "已配置";
              notify("项目工作目录已保存在本机，不会上传到协作服务。");
            },
          ),
        );
      else {
        const configure = button(
          workspaceState.get(node) ? "已使用本机目录" : "使用默认目录",
          async () => {
            configure.disabled = true;
            try {
              const result = await api("workspace-configure", {
                project: S.project,
                node,
                server,
              });
              if (result.configured && !result.pending) {
                workspaceState.set(node, true);
                status.className = "success small";
                status.textContent = "已配置";
                configure.textContent = "已使用本机目录";
                notify("目标机器已创建本机默认工作目录；目录路径不会上传。 ");
              } else {
                status.className = "pending small";
                status.textContent = "配置中";
                notify("配置指令已发送，目标机器在线后会在本机创建默认目录。 ");
              }
            } finally {
              configure.disabled = workspaceState.get(node) || false;
            }
          },
        );
        configure.disabled = !!workspaceState.get(node);
        block.append(configure);
        note(
          block,
          "目标机器会在本机创建默认目录；绝对路径不会经过协作服务。 ",
        );
      }
      b.append(block);
    });
    eligible.forEach((e) => {
      const row = el(
          "label",
          "setting-row",
          e.name + " · " + deviceName(e.node, server),
        ),
        check = el("input", "check");
      check.type = "checkbox";
      const assigned = S.view.members.find(
        (m) => m.employee_id === e.id || m.id === e.id,
      );
      check.checked = !!assigned;
      check.disabled = !!e.pending?.length;
      row.append(check);
      b.append(row);
      checks.push([e, check, !!assigned]);
    });
    a.append(
      button("取消", closeModal),
      button(
        "确认派遣",
        async () => {
          const missing = checks.find(
            ([e, ch, old]) => ch.checked && !old && !workspaceState.get(e.node),
          );
          if (missing)
            throw Error(
              "请先为 " +
                deviceName(missing[0].node, server) +
                " 设置项目工作目录",
            );
          for (const [e, ch, old] of checks)
            if (ch.checked !== old)
              await api(
                ch.checked ? "employee-dispatch" : "employee-withdraw",
                { id: e.id, project: S.project },
              );
          closeModal();
          notify("派遣变更已提交，等待目标设备确认。");
          await refresh(true);
        },
        "primary",
      ),
    );
  });
}
