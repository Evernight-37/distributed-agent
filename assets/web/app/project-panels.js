"use strict";

function openProjectSettings(view) {
  const { id: project, name, archived_at: archived } = view.project;
  modal(t("项目设置", "Project settings"), (body, actions) => {
    body.append(el("h3", "", name));
    note(
      body,
      archived
        ? t(
            "此项目已归档，恢复后可继续聊天和派工。",
            "This project is archived. Restore it to resume chats and scheduling.",
          )
        : t(
            "归档会暂停派工，保留项目历史。正在运行的任务可以完成或取消。",
            "Archiving pauses scheduling and keeps project history. Runs already in progress can finish or be cancelled.",
          ),
    );
    actions.append(button(t("关闭", "Close"), closeModal));
    if (archived)
      actions.append(
        button(t("永久删除项目", "Delete project permanently"), () =>
          previewProjectDeletion(view),
        ),
      );
    actions.append(
      button(
        archived
          ? t("恢复项目", "Restore project")
          : t("归档项目", "Archive project"),
        async () => {
          await api("project-lifecycle", {
            project,
            action: archived ? "restore" : "archive",
          });
          closeModal();
          await refresh(true);
        },
        "primary",
      ),
    );
  });
}

async function previewProjectDeletion(view) {
  const project = view.project.id;
  const preview = await api("project-lifecycle", {
    project,
    action: "delete-preview",
  });
  modal(t("永久删除项目", "Delete project permanently"), (body, actions) => {
    body.append(el("h3", "", preview.name));
    note(
      body,
      t(
        "删除以下协作数据。本地工作目录单独保留。",
        "The collaboration records below will be deleted. Local working directories are retained.",
      ),
    );
    const labels = {
      messages: t("聊天消息", "Messages"),
      tasks: t("任务与草稿", "Tasks and drafts"),
      runs: t("运行记录", "Runs"),
      memory: t("记忆条目", "Memory entries"),
      attachments: t("附件", "Attachments"),
    };
    Object.entries(labels).forEach(([key, label]) =>
      body.append(
        el("div", "setting-row", `${label} · ${preview.counts[key]}`),
      ),
    );
    actions.append(button(t("返回", "Back"), () => openProjectSettings(view)));
    if (preview.busy) {
      note(
        body,
        t(
          "还有运行或设备配置在处理中。请等待完成，或取消运行后再试。",
          "A run or device change is still in progress. Wait for it to finish or cancel the run before trying again.",
        ),
      );
      return;
    }
    const confirmation = field(
      body,
      t("输入项目全名", "Enter the full project name"),
    );
    const remove = button(
      t("确认永久删除", "Confirm permanent deletion"),
      async () => {
        await api("project-lifecycle", {
          project,
          action: "delete",
          confirmation: confirmation.value,
        });
        closeModal();
        renderDashboard();
        await refresh(true);
      },
      "danger",
    );
    remove.disabled = true;
    confirmation.oninput = () => {
      remove.disabled = confirmation.value !== preview.name;
    };
    actions.append(remove);
  });
}

function projectMemoryKinds() {
  return [
    ["rule", t("长期规则", "Rule")],
    ["decision", t("已确认的决策", "Decision")],
    ["fact", t("可复用的事实", "Fact")],
  ];
}

function projectMemoryEntry(view, item) {
  const row = el("div", "memory-note"),
    author =
      view.users.find((u) => u.id === item.author_id)?.username ||
      view.members.find((agent) => agent.id === item.author_id)?.name ||
      t("项目成员", "Project member");
  const label = projectMemoryKinds().find(([id]) => id === item.kind)?.[1];
  row.append(
    el(
      "div",
      "small muted",
      [label, author, date(item.created_at)].filter(Boolean).join(" · "),
    ),
  );
  const report = el("div", "message-text");
  FoolshipRender.render(report, item.body);
  row.append(report);
  const sources = item.sources?.length ? item.sources : [item];
  const ids = [
    ...new Set(
      sources.map((source) => source.source_message_id).filter(Boolean),
    ),
  ];
  appendProjectMessageReferences(
    row,
    view,
    ids,
    t("来源消息", "Source messages"),
  );
  if (!ids.length)
    sources
      .filter((source) => source.source_task_id)
      .forEach((source) => {
        const task = (view.tasks || []).find(
          (item) => item.id === source.source_task_id,
        );
        if (task)
          row.append(
            button(
              t("查看来源运行", "View source run"),
              () => openProjectTaskTrace(view, task),
              "link small",
            ),
          );
      });
  return row;
}

function openProjectMemory(view, mode = "notes") {
  const project = view.project.id;
  const memory = view.project_memory || {};
  const legacy = !(view.capabilities || []).includes(
    "project_memory_review_v1",
  );
  const canReview = !view.project.archived_at && memory.can_review !== false;
  const panels = [
    ["notes", t("正式记忆", "Active memory")],
    ["candidates", t("待确认", "Pending review")],
    ["archived", t("已归档", "Archived")],
  ];
  modal(t("项目记忆", "Project memory"), (body, actions) => {
    const tabs = el("nav", "project-inspector-actions project-modal-tabs");
    if (!legacy) {
      for (const [key, label] of panels) {
        const tab = button(
          `${label} (${memory[key]?.length || 0})`,
          () => openProjectMemory(view, key),
          key === mode ? "primary" : "",
        );
        if (key === mode) tab.setAttribute("aria-current", "true");
        tabs.append(tab);
      }
      body.append(tabs);
    }
    const entries = memory[mode] || [];
    if (!entries.length) empty(body, t("暂无条目", "No entries"));
    const selected = new Set();
    const review = async (ids, action, extra = {}) => {
      await api("memory-note-review", { project, ids, action, ...extra });
      closeModal();
      if (S.project === project) await refresh(true);
    };
    const mark = () => {
      keep.disabled = archive.disabled =
        selected.size === 0 || selected.size > 30;
      merge.disabled = selected.size < 2 || selected.size > 30;
    };
    const keep = button(
      t("确认所选", "Confirm selected"),
      () => {
        const ids = [...selected];
        modal(
          t("确认项目记忆", "Confirm project memory"),
          (reviewBody, reviewActions) => {
            const kind = field(
              reviewBody,
              t("类别", "Category"),
              "fact",
              projectMemoryKinds(),
            );
            for (const item of entries.filter((note) => selected.has(note.id)))
              reviewBody.append(projectMemoryEntry(view, item));
            reviewActions.append(
              button(t("返回", "Back"), () => openProjectMemory(view, mode)),
              button(
                t("加入正式记忆", "Add to active memory"),
                () => review(ids, "keep", { kind: kind.value }),
                "primary",
              ),
            );
          },
        );
      },
      "primary",
    );
    const archive = button(t("归档所选", "Archive selected"), () =>
      review([...selected], "archive"),
    );
    const merge = button(t("合并所选", "Merge selected"), () => {
      const ids = [...selected];
      modal(
        t("合并记忆候选", "Merge memory candidates"),
        (mergeBody, mergeActions) => {
          const kind = field(
            mergeBody,
            t("类别", "Category"),
            "fact",
            projectMemoryKinds(),
          );
          const text = field(
            mergeBody,
            t("合并后的记忆", "Merged memory"),
            "",
            null,
            "textarea",
          );
          text.maxLength = 2000;
          for (const item of entries.filter((note) => selected.has(note.id)))
            mergeBody.append(projectMemoryEntry(view, item));
          mergeActions.append(
            button(t("返回", "Back"), () => openProjectMemory(view, mode)),
            button(
              t("确认合并", "Confirm merge"),
              () =>
                review(ids, "merge", {
                  body: text.value.trim(),
                  kind: kind.value,
                }),
              "primary",
            ),
          );
        },
      );
    });
    for (const item of entries) {
      const row = projectMemoryEntry(view, item);
      if (canReview && mode === "candidates") {
        const checkbox = el("input", "check");
        checkbox.type = "checkbox";
        checkbox.setAttribute(
          "aria-label",
          t(`选择记忆 ${item.id}`, `Select memory ${item.id}`),
        );
        checkbox.onchange = () => {
          if (checkbox.checked) selected.add(item.id);
          else selected.delete(item.id);
          mark();
        };
        row.prepend(checkbox);
      }
      if (
        canReview &&
        mode === "notes" &&
        [item.author_id, view.project.owner_id].includes(S.dash.user.id)
      )
        row.append(
          button(
            legacy ? t("删除", "Delete") : t("归档", "Archive"),
            async () => {
              if (!legacy) return review([item.id], "archive");
              await api("memory-note-delete", { project, id: item.id });
              closeModal();
              await refresh(true);
            },
            "small link",
          ),
        );
      body.append(row);
    }
    actions.append(button(t("关闭", "Close"), closeModal));
    if (canReview && mode === "candidates") {
      mark();
      actions.append(archive, merge, keep);
    }
    if (canReview && mode === "notes")
      actions.append(
        button(
          t("添加记忆", "Add note"),
          () => {
            modal(
              t("添加正式记忆", "Add active memory"),
              (noteBody, noteActions) => {
                const kind = legacy
                  ? null
                  : field(
                      noteBody,
                      t("类别", "Category"),
                      "fact",
                      projectMemoryKinds(),
                    );
                const content = field(
                  noteBody,
                  t("项目记忆内容", "Project memory note"),
                  "",
                  null,
                  "textarea",
                );
                content.maxLength = 2000;
                noteActions.append(
                  button(t("返回", "Back"), () => openProjectMemory(view)),
                  button(
                    t("保存记忆", "Save note"),
                    async () => {
                      await api("memory-note-add", {
                        project,
                        body: content.value.trim(),
                        ...(kind && { kind: kind.value }),
                      });
                      closeModal();
                      await refresh(true);
                    },
                    "primary",
                  ),
                );
              },
            );
          },
          "primary",
        ),
      );
  });
}

function runModel(details) {
  return details?.effective_model || details?.requested_model || "";
}

function appendRunDetails(parent, details = {}) {
  const usage = details.usage || {};
  const missing = t("未上报", "Not reported");
  const number = (value) =>
    Number.isFinite(value) ? value.toLocaleString(interfaceLocale()) : missing;
  const started = details.started_at,
    finished = details.finished_at;
  const elapsed =
    Number.isFinite(started) && Number.isFinite(finished) && finished >= started
      ? new Intl.NumberFormat(interfaceLocale(), {
          style: "unit",
          unit: "second",
          maximumFractionDigits: 1,
        }).format(finished - started)
      : missing;
  const hit =
    usage.input_tokens > 0 && Number.isFinite(usage.cached_input_tokens)
      ? new Intl.NumberFormat(interfaceLocale(), {
          style: "percent",
          maximumFractionDigits: 1,
        }).format(usage.cached_input_tokens / usage.input_tokens)
      : missing;
  const rows = [
    [
      details.effective_model
        ? t("实际模型", "Reported model")
        : t("请求模型", "Requested model"),
      runModel(details) || missing,
    ],
    [
      t("提交的思考强度", "Requested reasoning effort"),
      details.effective_reasoning_effort ||
        (Object.hasOwn(details, "effective_reasoning_effort")
          ? details.reasoning_effort === "medium"
            ? t("沿用会话或执行器设置", "Session or executor setting")
            : t("执行器默认", "Executor default")
          : missing),
    ],
    [t("执行耗时", "Run duration"), elapsed],
    [t("输入 token", "Input tokens"), number(usage.input_tokens)],
    [t("输出 token", "Output tokens"), number(usage.output_tokens)],
    [
      t("缓存读取 token", "Cached input tokens"),
      number(usage.cached_input_tokens),
    ],
    [t("缓存命中率", "Cache hit rate"), hit],
  ];
  for (const [label, value] of rows)
    parent.append(
      el("div", "setting-row project-run-detail", `${label} · ${value}`),
    );
}

function projectRunEntry(view, entry, compact = false) {
  const state = entry.state || entry.status;
  const who = view.members.find((agent) => agent.id === entry.agent_id);
  const issue = String(entry.error || "").includes(
    "No working Codex executable",
  )
    ? t("目标机器未找到 Codex CLI", "Codex CLI not found on the device")
    : ["queued", "starting", "running"].includes(state) && who?.online === false
      ? t("等待机器上线", "Waiting for the device to come online")
      : "";
  const status =
    entry.cancel_requested &&
    ["queued", "starting", "running", "unknown"].includes(state)
      ? t("等待接收端取消", "Waiting for the receiver to cancel")
      : projectExecutionDisplayStatus(entry);
  const title = entry.title || entry.template || entry.id;
  const row = el("div", "task project-inspector-row");
  row.append(
    el("strong", "", title),
    el(
      "span",
      "muted small",
      [who?.name, status, issue].filter(Boolean).join(" · "),
    ),
  );
  const actions = el("div", "project-inspector-actions");
  actions.append(
    button(
      t("运行详情", "Run details"),
      () => {
        if (entry.title) openProjectTaskTrace(view, entry);
        else
          modal(title, (body, footer) => {
            appendRunDetails(body, entry.run_details);
            footer.append(button(t("关闭", "Close"), closeModal));
          });
      },
      "link small",
    ),
  );
  if (entry.can_cancel && !entry.cancel_requested)
    actions.append(
      button(
        t("取消任务", "Cancel task"),
        () => confirmTaskCancellation(view.project.id, entry),
        "link small",
      ),
    );
  if (
    state === "unknown" &&
    entry.local_recovery &&
    (S.dash.devices || []).some(
      (d) => d.local && (d.node || d.id) === entry.local_recovery.node,
    )
  )
    actions.append(
      button(
        t("核对本机任务", "Review local task"),
        () => confirmLocalTaskStopped(view.project.id, entry),
        "link small",
      ),
    );
  row.append(actions);
  if (!compact && entry.created_at)
    row.append(el("span", "muted small", date(entry.created_at)));
  return row;
}

function openProjectRuns(view) {
  const entries = [...(view.tasks || []), ...(view.executions || [])].sort(
    (a, b) => b.created_at - a.created_at,
  );
  modal(t("运行记录", "Run history"), (body, actions) => {
    const filters = el("div", "project-run-filters");
    const modelNames = [
      ...new Set(
        entries.map((entry) => runModel(entry.run_details)).filter(Boolean),
      ),
    ];
    const agentField = el("div"),
      modelField = el("div");
    filters.append(agentField, modelField);
    const agent = field(agentField, t("Agent", "Agent"), "", [
      ["", t("所有 Agent", "All agents")],
      ...view.members.map((item) => [item.id, item.name]),
    ]);
    const model = field(modelField, t("模型", "Model"), "", [
      ["", t("所有模型", "All models")],
      ...modelNames.map((name) => [name, name]),
    ]);
    const list = el("div");
    const render = () => {
      const visible = entries.filter(
        (entry) =>
          (!agent.value || entry.agent_id === agent.value) &&
          (!model.value || runModel(entry.run_details) === model.value),
      );
      list.replaceChildren();
      const usage = visible
        .map((entry) => entry.run_details?.usage)
        .filter(Boolean);
      if (usage.length) {
        const totals = {};
        for (const key of [
          "input_tokens",
          "output_tokens",
          "cached_input_tokens",
        ])
          if (usage.some((value) => Number.isFinite(value[key])))
            totals[key] = usage.reduce(
              (sum, value) => sum + (value[key] || 0),
              0,
            );
        const matched = usage.filter(
          (value) =>
            value.input_tokens > 0 &&
            Number.isFinite(value.cached_input_tokens),
        );
        const hit = matched.length
          ? matched.reduce((sum, value) => sum + value.cached_input_tokens, 0) /
            matched.reduce((sum, value) => sum + value.input_tokens, 0)
          : null;
        const labels = {
          input_tokens: t("输入", "Input"),
          output_tokens: t("输出", "Output"),
          cached_input_tokens: t("缓存读取", "Cached input"),
        };
        const summary = Object.entries(totals).map(
          ([key, value]) =>
            `${labels[key]} ${value.toLocaleString(interfaceLocale())}`,
        );
        if (hit !== null)
          summary.push(
            `${t("命中率", "Hit rate")} ${new Intl.NumberFormat(interfaceLocale(), { style: "percent", maximumFractionDigits: 1 }).format(hit)}`,
          );
        list.append(
          el(
            "p",
            "note",
            t(
              `可见记录中 ${usage.length}/${visible.length} 条已上报用量`,
              `${usage.length}/${visible.length} visible runs reported usage`,
            ) + ` · ${summary.join(" · ")}`,
          ),
        );
      }
      if (!visible.length) empty(list, t("暂无运行记录", "No runs yet"));
      visible.forEach((entry) => list.append(projectRunEntry(view, entry)));
    };
    agent.onchange = model.onchange = render;
    body.append(filters, list);
    render();
    actions.append(button(t("关闭", "Close"), closeModal));
  });
}
