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
function projectConversationTitle(conversation) {
  if (
    conversationKind(conversation) === "group" &&
    (!conversation.title || conversation.title === "项目大群")
  )
    return t("项目大群", "Project chat");
  return conversation.title || t("未命名会话", "Untitled conversation");
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
  const fields = el("div", "project-task-fields");
  body.append(fields);
  const place = (label, value, size, options = null, type = "text") => {
    const cell = el("div", "project-task-field " + size);
    fields.append(cell);
    return field(cell, label, value, options, type);
  };
  return {
    title: place(t("标题", "Title"), task.title || "", "full"),
    instructions: place(
      t("工作说明", "Instructions"),
      task.instructions || task.body || "",
      "full",
      null,
      "textarea",
    ),
    deliverable: place(
      t("交付物", "Deliverable"),
      task.deliverable || "",
      "half",
      null,
      "textarea",
    ),
    acceptance: place(
      t("验收标准", "Acceptance criteria"),
      task.acceptance || "",
      "half",
      null,
      "textarea",
    ),
    validation: place(
      t("验证命令", "Verification command"),
      task.validation || "",
      "half",
    ),
    allowed_files: place(
      t("允许修改的文件", "Files allowed to change"),
      task.allowed_files || "",
      "half",
    ),
    agent: place(
      t("负责人", "Assignee"),
      task.agent || task.agent_id || "",
      "third",
      [
        ["", t("选择 Agent", "Select an agent")],
        ...(view.members || []).map((agent) => [
          agent.id,
          agent.name || agent.id,
        ]),
      ],
    ),
    stage: place(t("阶段", "Stage"), task.stage || "implement", "third", [
      ["discuss", t("讨论", "Discuss")],
      ["implement", t("实现", "Implement")],
    ]),
    visibility: place(
      t("可见范围", "Visibility"),
      task.visibility || "group",
      "third",
      [
        ["group", t("项目成员", "Project members")],
        ["private", t("仅相关成员", "Participants only")],
      ],
    ),
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
function projectWorkStatus(status, draft = false) {
  if (draft)
    return status === "confirmed"
      ? t("已确认", "Confirmed")
      : t("草稿", "Draft");
  return (
    {
      queued: t("排队中", "Queued"),
      running: t("执行中", "Running"),
      waiting: t("等待中", "Waiting"),
      delivered: t("已交付", "Delivered"),
      accepted: t("已验收", "Accepted"),
      blocked: t("受阻", "Blocked"),
    }[status] ||
    status ||
    t("未知", "Unknown")
  );
}
function projectExecutionStatus(status) {
  return (
    {
      queued: t("排队中", "Queued"),
      starting: t("正在启动", "Starting"),
      running: t("执行中", "Running"),
      waiting: t("等待中", "Waiting"),
      needs_review: t("待核对", "Needs review"),
      unknown: t("待核对", "Needs review"),
      delivered: t("已交付", "Delivered"),
      completed: t("已完成", "Completed"),
      cancelled: t("已取消", "Cancelled"),
      failed: t("失败", "Failed"),
      timed_out: t("已超时", "Timed out"),
      blocked: t("受阻", "Blocked"),
    }[status] ||
    status ||
    ""
  );
}
function projectExecutionDisplayStatus(entry) {
  const status = entry.display_status;
  if (status === "已处理 · 未发言")
    return t("已处理 · 未发言", "Processed · No reply");
  if (status === "已回复") return t("已回复", "Replied");
  if (status === "待核对") return t("待核对", "Needs review");
  return status || projectExecutionStatus(entry.state || entry.status);
}
function projectWorkStatusReason(reason) {
  if (reason === "附件等待设备所有者批准。")
    return t(reason, "Attachments are waiting for device owner approval.");
  if (reason === "附件交付未完成。查看消息中的原因，然后重试。")
    return t(
      reason,
      "Attachment delivery failed. Check the message for details, then retry.",
    );
  if (reason === "原设备的执行状态待确认。确认前此任务不会重复派工。")
    return t(
      reason,
      "The original device's status needs review. This task will not be assigned again until it is confirmed.",
    );
  if (reason === "执行 Agent 已不存在或其所有者已离开项目")
    return t(
      reason,
      "The assigned agent is gone or its owner has left the project.",
    );
  if (reason === "执行 Agent 当前未启用")
    return t(reason, "The assigned agent is paused.");
  if (reason === "工作范围版本缺失，请重新创建工作")
    return t(reason, "The scope version is missing. Create the task again.");
  if (
    reason ===
    "上一项任务的工作区仍有未提交修改。请先保存其变更，再重新运行此任务。"
  )
    return t(
      reason,
      "The previous task left uncommitted workspace changes. Save them before rerunning this task.",
    );
  return reason;
}
function projectWorkCard(view, kind, record, options = {}) {
  const draft = kind === "draft",
    assigneeId = record.agent || record.agent_id,
    assignee = (view.members || []).find((item) => item.id === assigneeId),
    deliveries = record.deliveries || [],
    delivery =
      options.event === "delivered"
        ? deliveries.find((item) => item.message_id === options.message?.id)
        : options.event === "waiting"
          ? null
          : deliveries.at(-1),
    event = {
      created: t("已创建", "Created"),
      draft_saved: t("已保存", "Saved"),
      draft_revised: t("已修订", "Revised"),
      confirmed: t("已确认", "Confirmed"),
      follow_up: t("有新要求", "Follow-up added"),
      waiting: t("待答复", "Awaiting reply"),
      delivered: t("已交付", "Delivered"),
      accepted: t("已验收", "Accepted"),
      reopened: t("已重新打开", "Reopened"),
      scope_updated: t("范围已更新", "Scope updated"),
    }[options.event],
    card = el("section", "project-work-card " + kind);
  card.setAttribute("data-work-id", String(record.id));
  card.append(
    el(
      "div",
      "project-work-card-kicker",
      (draft ? t("工作草稿", "Task draft") : t("项目工作", "Task")) +
        (event ? " · " + event : ""),
    ),
    el(
      "strong",
      "project-work-card-title",
      record.title || t("未命名工作", "Untitled task"),
    ),
  );
  const facts = el("div", "project-work-card-facts");
  if (options.compact) card.className += " project-work-card-compact";
  [
    [t("状态", "Status"), projectWorkStatus(record.status, draft), true],
    ...(!options.compact
      ? [
          [
            t("版本", "Version"),
            record.revision == null ? "—" : "v" + record.revision,
          ],
        ]
      : []),
    [
      t("负责人", "Assignee"),
      assignee?.name || assigneeId || t("未指派", "Unassigned"),
    ],
  ].forEach(([label, value, isStatus]) => {
    const fact = el("span", "", label + " · " + value);
    if (isStatus) {
      fact.className = "project-work-status";
      fact.setAttribute("data-status", record.status || "");
    }
    facts.append(fact);
  });
  card.append(facts);
  if (!options.compact)
    [
      [t("任务目标", "Goal"), record.instructions || record.body],
      [t("验收标准", "Acceptance criteria"), record.acceptance],
      [t("预期交付", "Expected deliverable"), record.deliverable],
      [
        options.event === "delivered"
          ? t("本次交付", "Delivery")
          : t("最新结果", "Latest result"),
        delivery?.summary ||
          delivery?.body ||
          projectWorkStatusReason(record.status_reason),
        true,
      ],
      [
        t("等待答复", "Awaiting reply"),
        options.event === "waiting" && options.message?.body,
        true,
      ],
    ].forEach(([label, value, isResult]) => {
      if (!value) return;
      const row = el(
        "div",
        "project-work-card-field" +
          (isResult ? " project-work-card-result" : ""),
      );
      row.append(
        el("span", "project-work-card-label", label),
        el("p", "", value),
      );
      card.append(row);
    });
  if (options.controls !== false)
    card.append(
      button(
        draft
          ? record.status === "draft" && record.can_edit !== false
            ? t("审阅并确认", "Review and confirm")
            : t("查看草稿", "View draft")
          : t("查看详情", "View details"),
        () =>
          draft
            ? openProjectWorkForm(view, { draft: record })
            : openProjectWorkDetail(view, record),
        "link small project-work-card-action",
      ),
    );
  return card;
}
function openProjectWorkForm(view, options = {}) {
  const source = options.draft || options.task || {},
    project = view.project.id;
  modal(
    options.draft
      ? t("审阅任务草稿", "Review task draft")
      : t("创建任务", "Create task"),
    (body, actions) => {
      if (options.draft)
        note(
          body,
          t(
            `草稿 v${options.draft.revision}`,
            `Draft v${options.draft.revision}`,
          ) +
            " · " +
            (options.draft.can_edit === false
              ? t("仅供查看。", "Read-only.")
              : t(
                  "修改后请先保存，再确认新版本。",
                  "Save changes before confirming the new version.",
                )),
        );
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
        t("草稿依据", "Draft references"),
      );
      actions.append(button(t("取消", "Cancel"), closeModal));
      if (options.draft) {
        if (options.draft.can_edit === false) {
          Object.values(controls).forEach(
            (control) => (control.disabled = true),
          );
          return;
        }
        actions.append(
          button(t("继续讨论", "Discuss draft"), () =>
            openProjectWorkTextAction(
              t("继续讨论草稿", "Discuss the draft"),
              "draft-discuss",
              {
                project,
                draft_id: options.draft.id,
                revision: options.draft.revision,
              },
            ),
          ),
          button(
            t("保存新版本", "Save new version"),
            projectWorkHandler(
              "revise",
              () => {
                if (!changed())
                  throw Error(
                    t("任务内容没有变化", "There are no changes to save."),
                  );
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
                notify(
                  t(
                    "新版本已保存，请重新核对后确认。",
                    "New version saved. Review it before confirming.",
                  ),
                );
              },
            ),
          ),
          button(
            t("确认此版本", "Confirm this version"),
            projectWorkHandler(
              "confirm",
              () => {
                if (changed())
                  throw Error(
                    t(
                      "内容已修改，请先保存新版本并重新核对",
                      "Save and review the new version before confirming.",
                    ),
                  );
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
            t("创建", "Create"),
            projectWorkHandler(
              "create",
              () => {
                const values = projectWorkValues(controls);
                if (!values.title || !values.instructions || !values.agent)
                  throw Error(
                    t(
                      "请填写标题、工作说明并选择负责人",
                      "Enter a title and instructions, and select an assignee.",
                    ),
                  );
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
    },
  );
}
function openProjectWorkTextAction(title, action, target) {
  modal(title, (body, actions) => {
    const input = field(body, t("内容", "Message"), "");
    actions.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("提交", "Submit"),
        projectWorkHandler(
          action,
          () => {
            if (!input.value.trim())
              throw Error(t("请填写内容", "Enter a message."));
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
  modal(t("引用项目消息", "Reference a project message"), (body, actions) => {
    const input = field(body, t("消息编号", "Message ID"), "", null, "number");
    actions.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("引用", "Add reference"),
        projectWorkHandler(
          "reference",
          () => {
            const messageId = Number(input.value);
            if (!Number.isInteger(messageId))
              throw Error(t("请输入消息编号", "Enter a message ID."));
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
  modal(t("修改工作范围", "Edit task scope"), (body, actions) => {
    const controls = {
      title: field(body, t("标题", "Title"), work.title || ""),
      instructions: field(
        body,
        t("工作说明", "Instructions"),
        work.instructions || "",
      ),
      deliverable: field(
        body,
        t("交付物", "Deliverable"),
        work.deliverable || "",
      ),
      acceptance: field(
        body,
        t("验收标准", "Acceptance criteria"),
        work.acceptance || "",
      ),
      validation: field(
        body,
        t("验证命令", "Verification command"),
        work.validation || "",
      ),
      allowed_files: field(
        body,
        t("允许修改的文件", "Files allowed to change"),
        work.allowed_files || "",
      ),
    };
    const revision = work.revision;
    note(
      body,
      t(
        `当前版本 · ${revision}。保存时会核对版本，避免覆盖其他修改。`,
        `Current version · ${revision}. Saving checks for newer changes before updating the scope.`,
      ),
    );
    actions.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("保存范围", "Save scope"),
        projectWorkHandler(
          "update-scope",
          () => {
            const values = projectWorkValues(controls);
            if (!values.title || !values.instructions)
              throw Error(
                t("请填写标题和工作说明", "Enter a title and instructions."),
              );
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
  modal(work.title || t("项目工作", "Task"), (body, actions) => {
    const agent = (view.members || []).find(
      (item) => item.id === (work.agent || work.agent_id),
    );
    const status = projectWorkStatus(work.status);
    const revision = work.revision;
    body.append(
      el("div", "setting-row", t("状态 · ", "Status · ") + status),
      el("div", "setting-row", t("版本 · ", "Version · ") + (revision ?? "—")),
      el(
        "div",
        "setting-row",
        t("负责人 · ", "Assignee · ") +
          (agent?.name || work.agent || work.agent_id || ""),
      ),
      el("p", "", work.instructions || ""),
      el(
        "div",
        "setting-row",
        t("交付物 · ", "Deliverable · ") +
          (work.deliverable || t("未填写", "Not specified")),
      ),
      el(
        "div",
        "setting-row",
        t("验收标准 · ", "Acceptance criteria · ") +
          (work.acceptance || t("未填写", "Not specified")),
      ),
      el(
        "div",
        "setting-row",
        t("验证 · ", "Verification · ") +
          (work.validation || t("未填写", "Not specified")),
      ),
    );
    if (work.status_reason)
      body.append(
        el("p", "muted", projectWorkStatusReason(work.status_reason)),
      );
    (work.deliveries || []).forEach((delivery) => {
      const card = el(
        "div",
        "task",
        t("交付 · ", "Delivery · ") +
          (delivery.summary || delivery.body || delivery.id || ""),
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
          t("交付消息", "Delivery message"),
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
            t("补充信息 · ", "Additional context · ") +
              (input.body || input.kind || input.id || ""),
          ),
        );
    });
    appendProjectMessageReferences(
      body,
      view,
      referenceIds,
      t("引用消息", "Reference message"),
    );
    (work.executions || []).forEach((execution) => {
      const row = el(
        "div",
        "task",
        t("执行记录 · ", "Run · ") +
          (projectExecutionStatus(execution.state || execution.status) ||
            execution.id ||
            ""),
      );
      if (execution.error)
        row.append(el("div", "error small", execution.error));
      row.append(
        button(
          t("运行详情", "Run details"),
          () => {
            const cached = (view.tasks || []).find(
              (item) => item.id === execution.task_id,
            );
            if (cached) openProjectTaskTrace(view, cached);
            else
              modal(t("运行详情", "Run details"), (runBody, footer) => {
                appendRunDetails(runBody, execution.run_details);
                footer.append(button(t("关闭", "Close"), closeModal));
              });
          },
          "link small",
        ),
      );
      body.append(row);
    });
    const textAction = (label, title, action) =>
      button(label, () =>
        openProjectWorkTextAction(title, action, {
          project,
          work_id: work.id,
        }),
      );
    actions.append(button(t("关闭", "Close"), closeModal));
    if (work.can_manage !== false && work.status !== "accepted")
      actions.append(
        button(t("修改范围", "Edit scope"), () =>
          openProjectWorkScope(project, work),
        ),
      );
    if (
      work.can_manage !== false &&
      ["queued", "running", "waiting", "blocked", "delivered"].includes(
        work.status,
      )
    )
      actions.append(
        textAction(
          t("继续", "Follow up"),
          t("继续项目工作", "Follow up on task"),
          "follow-up",
        ),
      );
    if (work.can_manage !== false)
      actions.append(
        button(t("引用消息", "Reference message"), () =>
          openProjectWorkReference(project, work),
        ),
      );
    if (work.can_manage !== false && work.status === "delivered")
      actions.append(
        button(
          t("验收", "Accept delivery"),
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
      actions.append(
        textAction(
          t("重新打开", "Reopen"),
          t("重新打开项目工作", "Reopen task"),
          "reopen",
        ),
      );
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
    [
      "",
      addressedWake
        ? t("普通聊天", "Chat")
        : t("默认由管理员回复", "Admin replies by default"),
    ],
    ...view.members.map((agent) => [
      agent.id,
      (agent.id === view.project.admin_agent_id
        ? t("交给管理员 · @", "Send to admin · @")
        : "@") + (agent.name || agent.id),
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
    const text = (label || t("关联消息", "Related message")) + " · #" + id;
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
  modal(task.title || t("任务追溯", "Task history"), (body, actions) => {
    const id = field(body, t("任务编号", "Task ID"), task.id);
    id.readOnly = true;
    if (task.error) body.append(el("p", "error small", task.error));
    const metrics = el("details", "panel side-section");
    metrics.open = true;
    metrics.append(el("summary", "", t("运行与用量", "Run and usage")));
    appendRunDetails(metrics, task.run_details);
    body.append(metrics);
    if (name)
      body.append(
        el("div", "setting-row", t("负责人 · ", "Assignee · ") + name),
      );
    if (trace.node)
      body.append(
        el(
          "div",
          "setting-row",
          t("执行设备 · ", "Device · ") + (trace.node_name || trace.node),
        ),
      );
    if (trace.source_message_id)
      body.append(
        el(
          "div",
          "setting-row",
          t("来源消息 · #", "Source message · #") + trace.source_message_id,
        ),
        ...(sourceRoom
          ? [
              button(
                t("查看来源消息", "View source message"),
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
          t("查看上游任务", "View parent task"),
          () => openProjectTaskTrace(view, parent),
          "link small",
        ),
      );
    if (deliveryRoom)
      body.append(
        button(
          t("交付会话 · ", "Delivery conversation · ") +
            projectConversationTitle(deliveryRoom),
          async () => {
            closeModal();
            await selectConversation(deliveryRoom);
          },
          "link small",
        ),
      );
    actions.append(
      button(t("关闭", "Close"), closeModal),
      button(
        t("复制任务编号", "Copy task ID"),
        async () => {
          await copyText(task.id, id);
          notify(t("已复制任务编号", "Task ID copied."));
        },
        "primary",
      ),
    );
  });
}

function confirmTaskCancellation(project, task) {
  modal(t("取消任务", "Cancel task"), (body, actions) => {
    body.append(el("p", "", task.title || task.id));
    note(
      body,
      t(
        "已交给设备的任务会在接收端确认后标记为已取消。",
        "If the task has reached a device, it is marked cancelled after the receiver confirms.",
      ),
    );
    actions.append(
      button(t("返回", "Back"), closeModal),
      button(
        t("确认取消任务", "Confirm cancellation"),
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
  modal(t("核对本机任务", "Review local task"), (body, actions) => {
    body.append(el("p", "", task.title || task.id));
    note(
      body,
      t(
        "请先在原执行电脑上检查任务进程及其子进程。确认都已停止后再继续。任务状态会在接收端回报后更新。",
        "On the device that ran the task, check that the task process and all child processes have stopped. The status updates after the receiver reports back.",
      ),
    );
    actions.append(
      button(t("返回", "Back"), closeModal),
      button(
        t("确认原任务已停止", "Confirm the task has stopped"),
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
  modal(
    approve
      ? t("批准本次提权", "Approve extra permissions")
      : t("拒绝本次提权", "Deny extra permissions"),
    (body, actions) => {
      body.append(el("p", "", agent.name));
      if (request.reason)
        body.append(el("p", "", projectPermissionReason(request.reason)));
      note(
        body,
        approve
          ? t(
              "只有这项任务的下一次续跑可以访问工作区外。续跑结束后恢复原有权限。",
              "This task can access files outside its workspace for one resumed run. Its original permissions apply afterward.",
            )
          : t(
              "这次申请会关闭，Agent 后续任务使用项目原有权限。",
              "The request will close. Future runs use the project's existing permissions.",
            ),
      );
      actions.append(
        button(t("返回", "Back"), closeModal),
        button(
          approve
            ? t("确认批准", "Confirm approval")
            : t("确认拒绝", "Confirm denial"),
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
    },
  );
}

function projectPermissionReason(reason) {
  return reason === "一条私有任务申请在本机单次提权。"
    ? t(
        reason,
        "A private task requests one-time extra permissions on this device.",
      )
    : reason;
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
  modal(t("开始私聊", "Start a direct message"), (body, actions) => {
    if (!options.length) {
      empty(
        body,
        t(
          "当前没有可以私聊的成员或 Agent。",
          "No members or agents are available to message.",
        ),
      );
      actions.append(button(t("关闭", "Close"), closeModal));
      return;
    }
    note(
      body,
      t(
        "你只能私聊自己的 Agent；人与人的私聊仅双方可见。",
        "You can message your own agents. Direct messages between people are visible to both participants only.",
      ),
    );
    options.forEach((target) => {
      const existing = (v.conversations || []).find(
        (c) =>
          conversationKind(c) === target.kind &&
          conversationParticipants(c).includes(target.id),
      );
      const label =
        (target.kind === "human_agent"
          ? "Agent · "
          : t("成员 · ", "Member · ")) + target.name;
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
    actions.append(button(t("取消", "Cancel"), closeModal));
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
    owner = p.owner_id === me,
    archived = !!p.archived_at;
  if (
    !Array.isArray(v.capabilities) ||
    !v.capabilities.includes("conversation_privacy_v2")
  ) {
    c.replaceChildren(
      head(p.name, t("项目对话", "Project conversations")),
      el(
        "div",
        "panel error",
        t(
          "协作服务版本过旧。请让服务所在电脑更新应用并重启团队服务，然后重新打开项目。为保护私聊，此版本不会显示旧服务返回的聊天内容。",
          "The team service is too old. Update Foolship on the host computer, restart the team service, then reopen the project. Messages from the older service are hidden to protect direct conversations.",
        ),
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
    readOnly = archived || kind === "agent_agent";
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
  const oldWork = c.querySelector("#projectWork"),
    workOpen = oldWork?.open ?? true;
  const oldMembers = c.querySelector("#projectMembers"),
    membersOpen = oldMembers?.open || false;
  const panelScroll = {
    rail: c.querySelector(".conversation-rail")?.scrollTop || 0,
    roomList: c.querySelector(".project-room-list")?.scrollLeft || 0,
    side: c.querySelector(".project-side")?.scrollTop || 0,
    work: oldWork?.scrollTop || 0,
    members: oldMembers?.scrollTop || 0,
  };
  c.dataset.projectId = S.project;
  c.dataset.conversationId = selectedId;
  const hasProjectWork = (v.capabilities || []).includes("project_work_v1"),
    header = head(
      p.name,
      [
        serviceName(v.server || p.server),
        archived ? t("已归档", "Archived") : "",
      ]
        .filter(Boolean)
        .join(" · "),
    );
  header.classList.add("project-workspace-header");
  const projectActions = el("div", "project-inspector-actions");
  if (owner && v.capabilities.includes("project_lifecycle_v1"))
    projectActions.append(
      button(t("项目设置", "Project settings"), () => openProjectSettings(v)),
    );
  if (hasProjectWork && !archived)
    projectActions.append(
      button(
        t("＋ 新建任务", "＋ New task"),
        () => openProjectWorkForm(v, createWorkOptions),
        "primary",
      ),
    );
  if (projectActions.childElementCount) header.append(projectActions);
  c.replaceChildren(header);
  const layout = el("div", "conversation-layout project-workspace");
  const rooms = el("aside", "panel conversation-rail");
  rooms.setAttribute("aria-label", t("项目会话", "Project conversations"));
  const roomHead = el("div", "project-room-heading"),
    roomList = el("div", "project-room-list");
  roomHead.append(el("h2", "", t("会话", "Chats")));
  if (!archived)
    roomHead.append(
      button(
        t("＋ 私聊", "＋ DM"),
        startDirectConversation,
        "project-room-create",
      ),
    );
  rooms.append(roomHead, roomList);
  function roomButton(conversation) {
    const b = button(
      "",
      () => selectConversation(conversation),
      "conversation-item" + (conversation.id === selectedId ? " active" : ""),
    );
    const label = projectConversationTitle(conversation);
    const title = el("span", "conversation-title", label);
    title.title = label;
    if (conversation.id === selectedId) b.setAttribute("aria-current", "true");
    const icon = el(
      "span",
      "project-room-icon",
      conversationKind(conversation) === "group" ? "#" : "",
    );
    icon.setAttribute("aria-hidden", "true");
    b.append(icon, title);
    const count = Number(conversation.unread_count || 0);
    if (count > 0)
      b.append(el("span", "unread", String(count > 99 ? "99+" : count)));
    return b;
  }
  roomList.append(roomButton(group));
  const myChats = conversations.filter((x) =>
    ["human_agent", "human_human"].includes(conversationKind(x)),
  );
  if (myChats.length) {
    roomList.append(
      el("div", "conversation-label", t("我的私聊", "Direct messages")),
    );
    myChats.forEach((x) => roomList.append(roomButton(x)));
  }
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
  const summary = el("summary", "", t("Agent 内部会话", "Agent conversations"));
  if (unread > 0)
    summary.append(el("span", "unread", String(unread > 99 ? "99+" : unread)));
  internal.append(summary);
  if (internals.length)
    internals.forEach((x) => internal.append(roomButton(x)));
  else
    internal.append(
      el(
        "div",
        "muted small conversation-hint",
        t("暂无内部会话", "No agent conversations yet"),
      ),
    );
  rooms.append(internal);
  const chat = el("section", "panel chat");
  const chatHead = el("div", "chat-head");
  const chatTitle = el("div", "project-chat-title");
  chatTitle.append(el("h2", "", projectConversationTitle(selected)));
  const privacy = {
    group: t("所有项目成员可见", "Visible to all project members"),
    human_agent: t("仅你和该 Agent 可见", "Visible only to you and this agent"),
    human_human: t("仅对话双方可见", "Visible only to the participants"),
    agent_agent: t(
      "项目成员可旁观，其他 Agent 不可见",
      "Project members can read this. Other agents cannot.",
    ),
  };
  chatTitle.append(el("span", "muted small", privacy[kind] || ""));
  chatHead.append(
    chatTitle,
    button(
      t("任务与成员", "Tasks and members"),
      () => {
        const inspector = c.querySelector(".project-side");
        inspector?.scrollIntoView({ block: "start" });
        inspector?.focus({ preventScroll: true });
      },
      "project-inspector-jump",
    ),
  );
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
    empty(
      msgs,
      isGroup
        ? t("在项目大群开始讨论。", "Start a conversation with the project.")
        : t("这段会话还没有消息。", "No messages yet."),
    );
  const tasksById = new Map((v.tasks || []).map((task) => [task.id, task]));
  const firstClassWorkCards = (v.capabilities || []).includes(
    "project_work_cards_v1",
  );
  const draftsById = new Map(
    (v.work?.drafts || []).map((draft) => [String(draft.id), draft]),
  );
  const workById = new Map(
    (v.work?.items || []).map((work) => [String(work.id), work]),
  );
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
    const baseSender =
        m.sender_type === "system"
          ? t("系统", "System")
          : m.sender_name || m.sender_username || m.sender_id || "?",
      clientLabel = { codex: "Codex", dsh: "DSH" }[m.sender_client],
      row = el("div", "message"),
      bubble = el("div", "bubble"),
      identity = el("div", "project-message-identity"),
      name = el("strong", "name", baseSender),
      metadata = el("div", "project-message-header");
    row.id = "project-message-" + m.id;
    row.tabIndex = -1;
    row.setAttribute("data-sender-type", m.sender_type || "human");
    identity.append(name);
    if (clientLabel)
      identity.append(el("span", "project-message-client", clientLabel));
    if (m.mention_agent_name)
      identity.append(el("span", "channel", "@" + m.mention_agent_name));
    metadata.append(
      identity,
      el("time", "project-message-time", date(m.created_at)),
    );
    const cardMetadata = firstClassWorkCards && m.project_work_card,
      cardKind = cardMetadata?.kind,
      cardRecord =
        cardKind === "draft"
          ? draftsById.get(String(cardMetadata.id))
          : cardKind === "work"
            ? workById.get(String(cardMetadata.id))
            : null,
      body = el("div", "message-text");
    if (!cardRecord) {
      const request = m.permission_request;
      const requestAgent =
        request && v.members.find((agent) => agent.id === request.agent_id);
      const messageBody =
        m.sender_type === "system" &&
        request?.reason &&
        requestAgent?.owner_id === me
          ? t(
              m.body,
              `${requestAgent.name} requests extra permissions for a resumed run: ${projectPermissionReason(request.reason)}`,
            )
          : m.body;
      FoolshipRender.render(body, messageBody);
      streamMessageText(m, body, msgs);
    }
    bubble.append(metadata, body);
    if (cardRecord) {
      bubble.classList.add("work-card-message");
      bubble.append(
        projectWorkCard(v, cardKind, cardRecord, {
          event: cardMetadata.event,
          message: m,
        }),
      );
    }
    const actions = el("div", "project-message-actions"),
      linkedTask = tasksById.get(m.task_id);
    if (linkedTask)
      actions.append(
        button(
          t("查看关联任务", "View related task"),
          () => openProjectTaskTrace(v, linkedTask),
          "link small",
        ),
      );
    (m.attachments || []).forEach((file) =>
      bubble.append(attachmentCard(file, S.project)),
    );
    if (
      !cardRecord &&
      (isGroup || privateWorkAgent) &&
      (v.capabilities || []).includes("project_work_v1")
    )
      actions.append(
        button(
          t("从此消息建任务", "Create task from message"),
          () =>
            openProjectWorkForm(v, {
              ...createWorkOptions,
              source_message_id: m.id,
              task: { ...createWorkOptions.task, instructions: m.body },
            }),
          "link small project-message-create",
        ),
      );
    if (!firstClassWorkCards)
      (workByMessage.get(m.id) || []).forEach(({ kind, record }) =>
        actions.append(
          button(
            kind === "draft"
              ? t("查看工作草稿", "View task draft")
              : t("查看项目工作", "View task"),
            () =>
              kind === "draft"
                ? openProjectWorkForm(v, { draft: record })
                : openProjectWorkDetail(v, record),
            "link small",
          ),
        ),
      );
    if (actions.childElementCount) bubble.append(actions);
    const request = m.permission_request;
    if (request?.id) {
      const agent = v.members.find((item) => item.id === request.agent_id);
      if (agent?.owner_id === me) {
        const state = {
          pending: t("等待机器主人批准", "Waiting for device owner approval"),
          approved: t("已批准一次续跑", "Approved for one resumed run"),
          consumed: t("单次授权已使用", "One-time approval used"),
          denied: t("申请已拒绝", "Request denied"),
        }[request.state];
        const permission = el("div", "project-message-permission");
        if (state) permission.append(el("strong", "", state));
        if (request.state === "pending") {
          const decisions = el("div", "project-inspector-actions");
          decisions.append(
            button(
              t("批准本次", "Approve once"),
              () => confirmPermissionRequest(p.id, agent, request, true),
              "small",
            ),
            button(
              t("拒绝", "Deny"),
              () => confirmPermissionRequest(p.id, agent, request, false),
              "small",
            ),
          );
          permission.append(decisions);
        }
        if (permission.childElementCount) bubble.append(permission);
      }
    }
    const avatar = el("span", "avatar", baseSender.slice(0, 1));
    avatar.setAttribute("aria-hidden", "true");
    row.append(avatar, bubble);
    msgs.append(row);
  });
  chat.append(msgs);
  if (readOnly) {
    chat.append(
      el(
        "div",
        "composer-readonly",
        archived
          ? t("项目已归档", "Project archived")
          : t("仅供查看", "Read-only"),
      ),
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
      text = el("textarea"),
      toolbar = el("div", "project-composer-toolbar"),
      tools = el("div", "project-composer-tools"),
      send = button(t("发送", "Send"), null, "primary project-send");
    let sending = false,
      composing = false;
    if (isGroup) {
      mention.setAttribute(
        "aria-label",
        t("群消息发送方式", "Project chat delivery mode"),
      );
      groupMentionOptions(v).forEach(([value, label]) => {
        const option = el("option", "", label);
        option.value = value;
        mention.append(option);
      });
      mention.value = draft.mention || "";
      tools.append(mention);
    }
    text.value = draft.body;
    text.rows = 2;
    const inputLabel = isGroup
      ? t("发消息给项目大群", "Message the project")
      : t("发私聊消息", "Write a direct message");
    text.placeholder =
      inputLabel +
      "\n" +
      t(
        "Enter 发送 · Shift+Enter 换行",
        "Enter to send · Shift+Enter for a new line",
      );
    text.setAttribute("aria-label", inputLabel);
    text.setAttribute("aria-keyshortcuts", "Enter Shift+Enter");
    send.type = "submit";
    const syncSend = () => {
      const current = S.drafts.get(draftKey);
      send.disabled =
        sending ||
        !!current?.uploading ||
        (!text.value.trim() && !current?.attachments?.length);
    };
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
      syncSend();
    };
    if (isGroup) mention.onchange = save;
    composer.append(text);
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
            button(t("移除", "Remove"), () => {
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
        syncSend();
      };
      const addFiles = async (incoming) => {
        const current = S.drafts.get(draftKey);
        if (!current || sending || current.uploading)
          throw new Error(
            t(
              "请等待当前文件处理完成",
              "Wait for the current attachments to finish.",
            ),
          );
        if (current.attachments.length + incoming.length > 5)
          throw new Error(
            t(
              "每条消息最多 5 个附件",
              "You can attach up to 5 files per message.",
            ),
          );
        if (incoming.some((file) => file.size > 5 * 1024 * 1024))
          throw new Error(
            t("每个附件最大 5 MB", "Each attachment can be up to 5 MB."),
          );
        current.uploading = true;
        syncSend();
        files.append(
          el(
            "span",
            "muted small",
            t("正在上传附件…", "Uploading attachments…"),
          ),
        );
        try {
          for (const file of incoming) {
            const encoded = await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () =>
                resolve(String(reader.result).split(",")[1]);
              reader.onerror = () =>
                reject(new Error(t("无法读取：", "Cannot read: ") + file.name));
              reader.readAsDataURL(file);
            });
            const metadata = await api("attachment-upload", {
              project: projectId,
              name: file.name || t("截图.png", "screenshot.png"),
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
      tools.append(
        button(
          t("添加附件", "Attach files"),
          () => picker.click(),
          "project-attach",
        ),
      );
      composer.append(picker, files);
      redrawFiles();
    } else {
      composer.append(
        el(
          "div",
          "muted small",
          t(
            "协作服务更新后可发送附件。",
            "Update the team service to send attachments.",
          ),
        ),
      );
    }
    toolbar.append(tools, send);
    composer.append(toolbar);
    syncSend();
    text.addEventListener("compositionstart", () => {
      composing = true;
    });
    text.addEventListener("compositionend", () => {
      composing = false;
    });
    text.onkeydown = (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing || composing) return;
      e.preventDefault();
      if (!send.disabled) composer.requestSubmit(send);
    };
    composer.onsubmit = (e) => {
      e.preventDefault();
      run(async () => {
        if (sending) return;
        const submission = S.drafts.get(draftKey) || draft;
        if (submission.uploading)
          throw new Error(
            t(
              "附件仍在上传，请稍后发送",
              "Wait for attachments to finish uploading before sending.",
            ),
          );
        const attached = submission.attachments || [];
        const body = text.value.trim() || (attached.length ? "📎 附件" : "");
        if (!body) return;
        sending = true;
        syncSend();
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
          syncSend();
        }
      });
    };
    chat.append(composer);
  }
  const side = el("aside", "project-side");
  side.tabIndex = -1;
  side.setAttribute("aria-label", t("任务与成员", "Tasks and members"));
  const sideHeading = (title, count) => {
    const summary = el("summary", "project-inspector-summary");
    summary.append(el("span", "", title));
    if (count)
      summary.append(el("span", "project-inspector-count", String(count)));
    return summary;
  };
  const inConversation = (item) =>
    !item.conversation_id || item.conversation_id === selectedId;
  if (hasProjectWork) {
    const projectWork = el("details", "panel side-section"),
      drafts = (v.work?.drafts || []).filter(
        (draft) => draft.status === "draft" && inConversation(draft),
      ),
      items = (v.work?.items || []).filter(inConversation);
    projectWork.id = "projectWork";
    projectWork.open = workOpen;
    projectWork.append(
      sideHeading(t("任务卡", "Tasks"), drafts.length + items.length),
    );
    if (!drafts.length && !items.length)
      projectWork.append(
        el(
          "p",
          "project-inspector-empty",
          t("此会话暂无任务", "No tasks in this conversation"),
        ),
      );
    drafts.forEach((draft) =>
      projectWork.append(projectWorkCard(v, "draft", draft, { compact: true })),
    );
    items.forEach((item) =>
      projectWork.append(projectWorkCard(v, "work", item, { compact: true })),
    );
    side.append(projectWork);
  }
  const runs = el("section", "panel side-section");
  runs.append(
    button(t("运行记录", "Run history"), () => openProjectRuns(v), "link"),
  );
  const attention = new Set([
    "queued",
    "starting",
    "running",
    "unknown",
    "failed",
    "timed_out",
    "awaiting_owner",
  ]);
  for (const entry of [...(v.tasks || []), ...(v.executions || [])]
    .filter((entry) => attention.has(entry.state || entry.status))
    .slice(0, 5))
    runs.append(projectRunEntry(v, entry, true));
  side.append(runs);
  const memory = el("section", "panel side-section");
  memory.append(
    button(t("项目记忆", "Project memory"), () => openProjectMemory(v), "link"),
  );
  const pendingMemory = v.project_memory?.candidates?.length || 0;
  if (pendingMemory)
    memory.append(
      button(
        t(`${pendingMemory} 条待确认`, `${pendingMemory} pending`),
        () => openProjectMemory(v, "candidates"),
        "small",
      ),
    );
  side.append(memory);
  const members = el("details", "panel side-section");
  members.id = "projectMembers";
  members.open = membersOpen;
  members.append(
    sideHeading(
      t("成员与 Agent", "Members and agents"),
      v.users.length + v.members.length,
    ),
  );
  v.users.forEach((u) =>
    members.append(
      el(
        "div",
        "member",
        u.username + (u.id === p.owner_id ? t(" · 群主", " · Owner") : ""),
      ),
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
              ? command.error || t("创建失败", "Couldn't create agent")
              : command.state === "applied"
                ? t("已创建，等待设备同步", "Created. Waiting for device sync")
                : t(
                    "等待目标设备创建",
                    "Waiting for the device to create the agent",
                  )),
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
          (a.id === p.admin_agent_id ? t(" · 管理员", " · Admin") : "") +
          (a.online ? "" : t(" · 机器离线", " · Device offline")),
      ),
    );
    if (!archived && a.can_manage_project_agent) {
      const command = agentCommands.find(
        (item) => item.backend_project === a.backend_project,
      );
      const pending = command && ["queued", "claimed"].includes(command.state);
      const configure = button(
        t("设置", "Settings"),
        () => openProjectAgentForm(a),
        "link small",
      );
      const remove = button(
        t("移除", "Remove"),
        () => removeProjectAgent(p.id, a),
        "link small",
      );
      configure.disabled = remove.disabled = !!pending;
      row.append(
        el(
          "span",
          "muted small",
          pending
            ? t("等待目标设备确认", "Waiting for device confirmation")
            : a.enabled
              ? t("已启用", "Enabled")
              : t("已暂停", "Paused"),
        ),
        configure,
        remove,
      );
      if (command?.state === "failed")
        row.append(
          el(
            "span",
            "muted small",
            command.error || t("操作失败", "Action failed"),
          ),
        );
    }
    members.append(row);
  });
  const memberActions = el(
    "div",
    "project-inspector-actions project-member-actions",
  );
  if (!archived)
    memberActions.append(
      button(
        t("新增 Agent", "Add agent"),
        () => openProjectAgentForm(),
        "small",
      ),
    );
  if (!archived && Array.isArray(v.mcp_approval_kinds))
    memberActions.append(
      button(
        t("MCP 审批权限", "MCP approval permissions"),
        () =>
          editMcpApprovalPolicy(
            p.id,
            v.mcp_approval_kinds,
            v.capabilities.includes("project_memory_review_v1"),
          ),
        "small",
      ),
    );
  if (owner && !archived)
    memberActions.append(
      button(t("邀请成员", "Invite member"), invite, "small"),
    );
  members.append(memberActions);
  if (owner && !archived) {
    const admin = field(
      members,
      t("管理员 Agent", "Admin agent"),
      p.admin_agent_id || "",
      [["", t("未设置", "Not set")], ...v.members.map((a) => [a.id, a.name])],
    );
    members.append(
      button(
        t("保存管理员", "Save admin"),
        async () => {
          await api("set-admin", {
            project: S.project,
            agent: admin.value,
          });
          await refresh(true);
        },
        "small",
      ),
    );
  }
  side.append(members);
  layout.append(rooms, chat, side);
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
    rooms.scrollTop = panelScroll.rail;
    roomList.scrollLeft = panelScroll.roomList;
    side.scrollTop = panelScroll.side;
    const workSection = side.querySelector("#projectWork");
    if (workSection) workSection.scrollTop = panelScroll.work;
    members.scrollTop = panelScroll.members;
  });
}
function editMcpApprovalPolicy(project, current, memoryReview = false) {
  modal(t("MCP 审批权限", "MCP approval permissions"), (body, actions) => {
    note(
      body,
      t(
        "勾选允许代言者在本项目中代你处理的事项。设备审批只适用于你自己的设备。",
        "Select what your MCP delegate may handle in this project. Device approvals apply to your own devices.",
      ),
    );
    const choices = [
      [
        "permission",
        t(
          "额外执行权限（一次续跑）",
          "Extra execution permissions (one resumed run)",
        ),
      ],
      ["attachment", t("发送附件", "Send attachments")],
      ["execution", t("执行已登记的命令", "Run registered commands")],
      ...(memoryReview
        ? [
            [
              "memory",
              t(
                "确认、合并或归档项目记忆",
                "Review, merge, or archive project memory",
              ),
            ],
          ]
        : []),
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
      button(t("返回", "Back"), closeModal),
      button(
        t("保存审批权限", "Save permissions"),
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
  modal(t("邀请成员", "Invite member"), (b, a) => {
    note(
      b,
      t(
        "对方加入后可在项目内创建自己的 Agent。",
        "Once they join, they can create their own agents in the project.",
      ),
    );
    a.append(
      button(t("关闭", "Close"), closeModal),
      button(
        t("生成邀请", "Generate invite"),
        async () => {
          const r = await api("invite", { project: S.project });
          b.replaceChildren();
          const code = field(
            b,
            t("项目邀请代码", "Project invite code"),
            r.invite,
          );
          code.readOnly = true;
          note(
            b,
            t(
              `协作服务：${r.server}。请把地址和邀请代码交给对方。`,
              `Team service: ${r.server}. Share this address and the invite code with the new member.`,
            ),
          );
          a.replaceChildren(button(t("完成", "Done"), closeModal, "primary"));
        },
        "primary",
      ),
    );
  });
}
function removeProjectAgent(project, agent) {
  modal(t("移除项目 Agent", "Remove project agent"), (body, actions) => {
    body.append(el("p", "", agent.name));
    note(
      body,
      t(
        "Agent 将停止在此项目接收新任务。工作文件、会话和任务历史会保留。请先结束正在执行的任务。",
        "The agent will stop receiving new tasks in this project. Work files, conversations, and task history are kept. Finish any running tasks first.",
      ),
    );
    actions.append(
      button(t("取消", "Cancel"), closeModal),
      button(
        t("确认移除", "Confirm removal"),
        async () => {
          await api("project-agent-delete", { project, agent_id: agent.id });
          closeModal();
          notify(t("移除请求已提交。", "Removal request submitted."));
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
    view.members.some(
      (item) =>
        item.node === node &&
        item.owner_id === user &&
        !item.agent_workspace_mode,
    )
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
  const managedModels = (view.capabilities || []).includes(
    "project_model_capabilities_v1",
  );

  modal(
    agent
      ? t("项目 Agent 设置", "Project agent settings")
      : t("新增项目 Agent", "Add a project agent"),
    (body, actions) => {
      actions.append(button(t("取消", "Cancel"), closeModal));
      if (!agent && !devices.length) {
        note(
          body,
          t(
            "请先将一台自己的机器接入该项目所在的协作服务。",
            "Connect one of your devices to this project's team service first.",
          ),
        );
        return;
      }
      const name = field(
        body,
        t("Agent 名称", "Agent name"),
        agent?.name || "",
      );
      name.maxLength = 120;
      let machine;
      if (agent) {
        note(
          body,
          t("执行机器 · ", "Device · ") + deviceName(agent.node, server),
        );
      } else {
        machine = field(body, t("执行机器", "Device"), "", [
          ["", t("选择已接入的机器", "Select a connected device")],
          ...devices.map((device) => [
            device.node || device.id,
            device.name +
              (device.local ? t(" · 本机", " · This device") : "") +
              (device.online ? "" : t(" · 离线", " · Offline")),
          ]),
        ]);
      }
      const selectedNode = () => agent?.node || machine.value;
      const selectedDevice = () =>
        devices.find((device) => (device.node || device.id) === selectedNode());
      const agentWorkspaces =
        !agent && view.capabilities?.includes("agent_workspace_v1");
      const mode =
        agentWorkspaces &&
        field(body, t("工作目录", "Working directory"), "dedicated", [
          [
            "dedicated",
            t("自动创建专属空目录", "Create a dedicated empty folder"),
          ],
          [
            "custom",
            t(
              "使用执行机器上的已有目录",
              "Use an existing folder on the device",
            ),
          ],
          ["shared", t("使用项目共享目录", "Use the shared project folder")],
        ]);
      let customPath;
      const workspace = el("div");
      const modelFields = el("div");
      body.append(workspace, modelFields);
      let model;
      const effort = field(
        body,
        t("思考强度", "Reasoning effort"),
        managedModels ? "default" : current.effort || "medium",
        (managedModels
          ? ["default"]
          : ["low", "medium", "high", "xhigh", "max", "ultra"]
        ).map((value) => [
          value,
          value === "default" ? t("执行器默认", "Executor default") : value,
        ]),
      );
      const modelStatus = el("div", "small muted");
      modelStatus.setAttribute("aria-live", "polite");
      body.append(modelStatus);
      const enabled = field(
        body,
        t("接收项目任务", "Receive project tasks"),
        String(current.enabled ?? true),
        [
          ["true", t("启用", "Enabled")],
          ["false", t("暂停", "Paused")],
        ],
      );
      const stageBox = el("div");
      stageBox.append(
        el("span", "label", t("允许的工作阶段", "Allowed work stages")),
      );
      const stages = [
        ["discuss", t("讨论", "Discuss")],
        ["review", t("审阅", "Review")],
        ["implement", t("实现", "Implement")],
      ].map(([value, label]) => {
        const row = el("label", "setting-row", label);
        const check = el("input", "check");
        check.type = "checkbox";
        check.checked = (current.stages || ["discuss", "review"]).includes(
          value,
        );
        row.append(check);
        stageBox.append(row);
        return [value, check];
      });
      body.append(stageBox);
      const timeout = field(
        body,
        t("每轮超时（秒）", "Turn timeout (seconds)"),
        current.timeout || 600,
        null,
        "number",
      );
      timeout.min = "30";
      timeout.max = "86400";
      const strict = field(
        body,
        t("实现任务安全模式", "Implementation safeguards"),
        String(!!current.strict_worktree),
        [
          ["false", t("关闭", "Off")],
          [
            "true",
            t(
              "仅允许 linked worktree 且限制变更文件",
              "Require a linked worktree and restrict changed files",
            ),
          ],
        ],
      );
      const attachment = field(
        body,
        t("附件发送权限", "Attachment permissions"),
        current.attachment_policy || "ask",
        [
          ["ask", t("请求批准", "Ask for approval")],
          ["auto", t("帮我批准", "Approve for me")],
          ["full", t("直接发送", "Send directly")],
        ],
      );
      const network = field(
        body,
        t("模型请求代理", "Model request proxy"),
        current.network?.mode || "machine",
        [
          ["machine", t("跟随执行机器设置", "Use device setting")],
          ["direct", t("直连（不走代理）", "Connect directly")],
          ["custom", t("自定义端口", "Custom port")],
        ],
      );
      const proxyPort = el("div", "project-agent-proxy-port");
      body.append(proxyPort);
      const port = field(
        proxyPort,
        t("代理端口", "Proxy port"),
        current.network?.port || "",
        null,
        "number",
      );
      port.min = "1";
      port.max = "65535";
      const proxyDevice = el("div", "small muted");
      proxyPort.append(proxyDevice);
      let customPort = port.value;
      const syncPort = () => {
        if (!port.disabled) customPort = port.value;
        port.disabled = network.value !== "custom";
        port.value = port.disabled ? "" : customPort;
        port.placeholder =
          network.value === "machine"
            ? t("沿用执行机器的默认设置", "Uses the device default")
            : network.value === "direct"
              ? t("直连无需填写端口", "Direct connection needs no port")
              : t("例如 7897", "e.g. 7897");
        const node = selectedNode();
        proxyDevice.hidden = network.value !== "machine" || !node;
        proxyDevice.textContent = node
          ? t("默认端口来自 ", "Default port from ") + deviceName(node, server)
          : "";
      };
      network.onchange = syncPort;
      syncPort();

      const workspaceStates = new Map();
      const submit = button(
        agent
          ? t("保存设置", "Save settings")
          : t("创建 Agent", "Create agent"),
        async () => {
          const node = selectedNode();
          const fields = {
            project,
            name: name.value.trim(),
            model: model?.value.trim(),
            effort: effort.value,
            ...(managedModels && { model_config_version: 2 }),
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
            throw Error(
              t(
                "请选择执行机器并填写名称和模型",
                "Select a device and enter a name and model.",
              ),
            );
          if (mode && mode.value !== "shared") {
            fields.workspace_mode = mode.value;
            if (mode.value === "custom") {
              fields.workspace_path = customPath.value.trim();
              if (!fields.workspace_path)
                throw Error(
                  t(
                    "请输入执行机器上的绝对路径",
                    "Enter an absolute path on the device",
                  ),
                );
            }
          } else if (!agent && workspaceStates.get(node) !== "ready")
            throw Error(
              t(
                "请先设置这个项目在目标机器上的工作目录",
                "Set the project's working directory on the target device first.",
              ),
            );
          if (!fields.stages.length)
            throw Error(
              t("请至少选择一个工作阶段", "Select at least one work stage."),
            );
          if (
            !Number.isInteger(fields.timeout) ||
            fields.timeout < 30 ||
            fields.timeout > 86400
          )
            throw Error(
              t(
                "每轮超时必须为 30 至 86400 秒",
                "Enter a turn timeout from 30 to 86,400 seconds.",
              ),
            );
          if (network.value === "custom") {
            const value = Number(port.value);
            if (!Number.isInteger(value) || value < 1 || value > 65535)
              throw Error(
                t(
                  "代理端口必须在 1 到 65535 之间",
                  "Enter a proxy port from 1 to 65,535.",
                ),
              );
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
            agent
              ? t("设置请求已提交。", "Settings request submitted.")
              : t(
                  "创建请求已提交，等待目标设备确认。",
                  "Creation request submitted. Waiting for device confirmation.",
                ),
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
          submit.disabled =
            !node || capabilities.get(node)?.state === "loading";
          return;
        }
        if (mode && mode.value !== "shared") {
          submit.disabled = false;
          if (mode.value === "custom")
            customPath = field(
              workspace,
              t("执行机器上的绝对路径", "Absolute path on the device"),
              "",
            );
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
          workspace.append(
            el(
              "div",
              "muted small",
              t("项目工作目录已设置", "Project working directory is ready"),
            ),
          );
          return;
        }
        const row = el("div", "note workspace-row");
        row.append(
          el(
            "span",
            "",
            state === "pending"
              ? t(
                  "目标设备正在配置项目工作目录",
                  "The device is setting up the project directory",
                )
              : t(
                  "请先设置项目工作目录",
                  "Set the project working directory first",
                ),
          ),
        );
        if (state === "pending") {
          row.append(
            button(t("检查配置状态", "Check status"), async () => {
              const latest = await api("project", { id: project });
              if (projectWorkspaceReady(latest, node, user))
                workspaceStates.set(node, "ready");
              renderWorkspace();
            }),
          );
        } else {
          row.append(
            button(
              selectedDevice()?.local
                ? t("选择目录", "Choose folder")
                : t("使用默认目录", "Use default folder"),
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

      const capabilities = new Map();
      let effortSelected = false;
      effort.onchange = () => {
        effortSelected = true;
      };

      function renderEfforts() {
        if (!managedModels) return;
        const data = capabilities.get(selectedNode());
        const id = model?.value.trim() || "";
        const record = data?.models?.find(
          (item) => item.name.toLowerCase() === id.toLowerCase(),
        );
        const choices = [
          "default",
          ...(record?.efforts || []).filter((level) => level !== "default"),
        ];
        const savedModel = current.model || agent?.model;
        const isSaved = !!agent && id === savedModel;
        const legacy = isSaved && current.model_config_version !== 2;
        const savedEffort = current.effort || agent?.effort;
        const knownDsh =
          legacy && (record?.executor || data?.default_executor) === "dsh";
        const legacyDsh = {
          low: "low",
          medium: "default",
          high: "high",
          xhigh: "high",
          max: "high",
          ultra: "high",
        };
        const previous = knownDsh ? legacyDsh[savedEffort] : savedEffort;
        const selected = effortSelected
          ? effort.value
          : isSaved
            ? previous
            : "default";
        effort.replaceChildren(
          ...choices.map((value) => {
            const label =
              value === "default"
                ? t("执行器默认", "Executor default") +
                  (record?.default_effort ? ` (${record.default_effort})` : "")
                : value;
            const option = el("option", "", label);
            option.value = value;
            return option;
          }),
        );
        effort.value = choices.includes(selected) ? selected : "default";
        const info = [];
        if (data?.state === "loading")
          info.push(
            t(
              "正在读取执行机器的模型能力…",
              "Reading model capabilities from the device…",
            ),
          );
        else if (id && !Array.isArray(record?.efforts))
          info.push(
            t(
              "这台机器未报告该模型的档位。",
              "This device did not report effort levels for this model.",
            ),
          );
        if (knownDsh && savedEffort && previous !== savedEffort)
          info.push(
            previous === "default"
              ? t(
                  `旧配置 ${savedEffort} 由原会话或执行器选择档位。`,
                  `Saved legacy effort ${savedEffort} uses the previous session or executor setting.`,
                )
              : t(
                  `旧配置 ${savedEffort} 当前按 ${previous} 运行。`,
                  `Saved legacy effort ${savedEffort} currently runs as ${previous}.`,
                ),
          );
        else if (
          isSaved &&
          savedEffort &&
          !choices.includes(savedEffort) &&
          data?.state !== "loading"
        )
          info.push(
            t(
              `已保存的档位 ${savedEffort} 需要重新选择。`,
              `Review the saved effort ${savedEffort} before applying changes.`,
            ),
          );
        const override =
          record?.legacy_model_override || data?.legacy_model_override;
        if (knownDsh && override && override !== savedModel)
          info.push(
            t(
              `旧设备配置当前使用 ${override}。保存后将使用 ${savedModel}。`,
              `The device's old configuration currently runs ${override}. Saving uses ${savedModel}.`,
            ),
          );
        modelStatus.textContent = info.join(" ");
      }

      function renderModel(saved = current.model || agent?.model || "") {
        modelFields.replaceChildren();
        const node = selectedNode();
        if (!node) {
          model = null;
          renderEfforts();
          return;
        }
        const available =
          capabilities.get(node)?.models ||
          (selectedDevice()?.local ? models : []);
        if (available.length || (selectedDevice()?.local && saved)) {
          const choices = available.map(({ name, executor }) => [
            name,
            name +
              " · " +
              (executor === "dsh" ? "DeepSeek Harness" : "Codex CLI"),
          ]);
          if (saved && !choices.some(([name]) => name === saved))
            choices.push([
              saved,
              saved + t(" · 当前配置", " · Current setting"),
            ]);
          model = field(modelFields, t("模型", "Model"), saved, [
            ["", t("选择模型", "Select a model")],
            ...choices,
          ]);
        } else {
          model = field(modelFields, t("模型 ID", "Model ID"), saved);
          note(
            modelFields,
            selectedDevice()?.local
              ? t(
                  "可在设置的本机可用模型列表中查看模型 ID。",
                  "Find the model ID in the available models list in Settings.",
                )
              : t(
                  "填写目标机器的可用模型列表中的模型 ID。",
                  "Enter a model ID from the target device's available models list.",
                ),
          );
        }
        if (managedModels) {
          model.oninput = () => {
            effortSelected = false;
            renderEfforts();
          };
          model.onchange = model.oninput;
          renderEfforts();
        }
      }

      async function fetchModels(node) {
        capabilities.set(node, { state: "loading" });
        renderEfforts();
        if (agent) renderWorkspace();
        try {
          let reply = await api("project-agent-models", { project, node });
          for (
            let tries = 0;
            reply.id &&
            ["queued", "claimed"].includes(reply.state) &&
            tries < 30;
            tries++
          ) {
            await new Promise((resolve) => setTimeout(resolve, 1200));
            if (!modelFields.isConnected) return;
            reply = await api("project-agent-models", {
              project,
              node,
              command_id: reply.id,
            });
          }
          capabilities.set(node, {
            ...reply.result,
            state: reply.state,
          });
        } catch {
          capabilities.set(node, { state: "unavailable" });
        }
        if (modelFields.isConnected && selectedNode() === node) {
          renderModel(model?.value.trim());
          if (agent) renderWorkspace();
        }
      }

      function renderMachine() {
        renderWorkspace();
        syncPort();
        effortSelected = false;
        renderModel();
        const node = selectedNode();
        if (managedModels && node && !capabilities.has(node)) fetchModels(node);
      }

      if (mode) mode.onchange = renderWorkspace;
      if (machine) machine.onchange = renderMachine;
      renderMachine();
    },
  );
}
