"use strict";

let transferPolling = false;
let transferPending = false;
let transferSignature = "";
let transferSnapshot = null;
let transferTarget = null;

function transfersHeading() {
  const title = manageHeading(t("文件速传", "File transfers"));
  title.dataset.transferRoot = "1";
  return title;
}

function directStatus(item) {
  const labels = {
    queued: item.incoming
      ? t("等待你确认", "Awaiting your approval")
      : t("等待对方确认", "Awaiting recipient approval"),
    accepted: t(
      "已确认，等待对方就绪",
      "Accepted, waiting for the other device",
    ),
    running: t("传输中", "Transferring"),
    done: t("已完成", "Complete"),
    failed: t("传输失败", "Transfer failed"),
    rejected: t("已拒绝", "Declined"),
  };
  return labels[item.state] || item.state;
}

function transferSettings(view) {
  const section = el("section", "panel manage-panel");
  const configured = !!view.settings?.receive_folder_configured;
  const folder = view.settings?.receive_folder || "";
  section.append(el("h2", "", t("接收设置", "Receiving settings")));
  const local = el("div", "manage-row manage-transfer-folder");
  const where = el("div", "manage-row-info");
  where.append(
    el(
      "strong",
      "",
      configured
        ? t("本机接收目录", "Receive folder")
        : t("默认接收目录", "Default receive folder"),
    ),
    el(
      "p",
      "manage-path",
      folder ||
        t(
          "默认目录不可用，请选择一个目录。",
          "The default folder is unavailable. Choose a folder.",
        ),
    ),
  );
  const controls = el("div", "manage-actions");
  controls.append(
    button(t("更换目录", "Change folder"), async () => {
      const result = await api("file-transfer-receive-folder");
      if (result.configured) {
        notify(t("接收目录已保存。", "Receive folder saved."));
        await renderTransfers("action");
      }
    }),
    button(t("打开目录", "Open folder"), async () => {
      const result = await api("file-transfer-open-folder");
      if (result?.opened)
        notify(t(`已打开：${result.opened}`, `Opened ${result.opened}`));
    }),
  );
  local.append(where, controls);
  section.append(local);
  const remoteDevices = (view.devices || []).filter(
    (device) =>
      !(S.dash?.devices || []).some(
        (candidate) =>
          candidate.local &&
          (candidate.node || candidate.id) === device.node &&
          candidate.server === device.server,
      ),
  );
  if (remoteDevices.length) {
    const list = el("div", "manage-list manage-transfer-devices");
    list.append(el("h3", "", t("其他机器", "Other devices")));
    remoteDevices.forEach((device) => {
      const row = el("div", "manage-row");
      const label = el("div", "manage-row-info");
      label.append(
        el("strong", "", device.name),
        el(
          "span",
          "manage-meta",
          serviceName(device.server) +
            (device.online
              ? t(" · 在线", " · Online")
              : t(" · 离线", " · Offline")),
        ),
      );
      row.append(
        label,
        button(t("设置接收目录", "Set receive folder"), () => {
          modal(
            t(
              `设置 ${device.name} 的接收目录`,
              `Receive folder on ${device.name}`,
            ),
            (body, actions) => {
              const destination = field(
                body,
                t("目标机器上的绝对路径", "Absolute path on the target device"),
                "",
              );
              destination.placeholder = t(
                "例如 /home/user/Downloads/Foolship 或 D:\\Transfers",
                "Example: /home/user/Downloads/Foolship or D:\\Transfers",
              );
              note(
                body,
                t(
                  "目标机器上线后接收设置并创建缺失目录。路径随签名指令发往协作服务，处理后从服务记录删除。",
                  "The target device applies this setting and creates the folder when it comes online. The signed instruction includes the path, which the coordination service removes after processing.",
                ),
              );
              actions.append(
                button(t("取消", "Cancel"), closeModal),
                button(
                  t("发送设置指令", "Send folder setting"),
                  async () => {
                    const result = await api("file-transfer-receive-folder", {
                      server: device.server,
                      node: device.node,
                      path: destination.value.trim(),
                    });
                    if (result.pending) {
                      closeModal();
                      notify(
                        t(
                          "设置指令已发送；目标机器在线后会创建并保存目录。",
                          "Setting sent. The target device will create and save the folder when it comes online.",
                        ),
                      );
                    }
                  },
                  "primary",
                ),
              );
            },
          );
        }),
      );
      list.append(row);
    });
    section.append(list);
  }
  return section;
}

function paintTransfers(content, view) {
  content.replaceChildren(transfersHeading());
  if (view.unavailable_services?.length)
    content.append(
      el(
        "div",
        "panel manage-panel manage-attention",
        t(
          "部分协作服务离线。恢复连接后会显示对应联系人和传输。",
          "Some coordination services are offline. Their contacts and transfers will appear when they reconnect.",
        ),
      ),
    );
  const layout = el("div", "manage-page manage-transfer-layout");
  const activity = el("div", "manage-transfer-main");
  activity.append(transferSend(view), transferDirect(view));
  layout.append(activity, transferSettings(view));
  content.append(layout);
}

function transferDirect(view) {
  const rows = [...(view.direct || [])].sort(
    (a, b) =>
      Number(Boolean(b.incoming && b.state === "queued")) -
      Number(Boolean(a.incoming && a.state === "queued")),
  );
  const section = el("section", "panel manage-panel");
  section.append(el("h2", "", t("传输记录", "Transfer history")));
  if (!rows.length) {
    section.append(
      el("p", "manage-empty", t("暂无传输。", "No transfers yet.")),
    );
    return section;
  }
  const list = el("div", "manage-list");
  rows.forEach((item) => {
    const row = el("div", "manage-row"),
      info = el("div", "manage-row-info");
    const stateClass =
      item.state === "done"
        ? "is-good"
        : item.state === "failed"
          ? "is-error"
          : item.state === "queued"
            ? "is-attention"
            : "";
    const title = el("div", "manage-transfer-title");
    title.append(
      el("strong", "", item.name),
      el("span", "manage-status " + stateClass, directStatus(item)),
    );
    info.append(
      title,
      el(
        "span",
        "manage-meta",
        (item.incoming ? t("来自 ", "From ") : t("发往 ", "To ")) +
          (item.peer_name || t("对方设备", "the other device")) +
          " · " +
          fileSize(item.size) +
          " · " +
          date(item.created_at),
      ),
    );
    if (item.state === "failed" && item.error)
      info.append(
        el("p", "manage-meta error", localizeDesktopText(item.error)),
      );
    if (item.state === "running" && item.size > 0) {
      const progress = el("progress", "manage-progress");
      progress.max = item.size;
      progress.value = Math.min(
        item.size,
        Math.max(0, Number(item.transferred) || 0),
      );
      progress.setAttribute(
        "aria-label",
        t(`${item.name}的传输进度`, `Transfer progress for ${item.name}`),
      );
      info.append(
        progress,
        el(
          "span",
          "manage-meta",
          `${fileSize(progress.value)} / ${fileSize(progress.max)}`,
        ),
      );
    }
    row.append(info);
    if (item.incoming && item.state === "queued") {
      const actions = el("div", "manage-actions");
      actions.append(
        button(t("拒绝", "Decline"), async () => {
          await api("file-transfer-direct-respond", {
            server: item.server,
            id: item.id,
            accept: false,
          });
          await renderTransfers("action");
        }),
        button(
          t("确认接收", "Accept transfer"),
          async () => {
            await api("file-transfer-direct-respond", {
              server: item.server,
              id: item.id,
              accept: true,
            });
            notify(t("已确认接收。", "Transfer accepted."));
            await renderTransfers("action");
          },
          "primary",
        ),
      );
      row.append(actions);
    }
    list.append(row);
  });
  section.append(list);
  return section;
}

function transferSend(view) {
  const targets = [];
  (view.devices || []).forEach((device) => {
    if (
      !(S.dash?.devices || []).some(
        (d) =>
          d.local &&
          (d.node || d.id) === device.node &&
          d.server === device.server,
      )
    )
      targets.push({
        server: device.server,
        recipient_id: S.dash.user.id,
        recipient_node: device.node,
        label: t("我的机器 · ", "My device · ") + device.name,
      });
  });
  (view.contacts || []).forEach((person) =>
    targets.push({
      server: person.server,
      recipient_id: person.id,
      recipient_node: null,
      label:
        person.username +
        t(" · 由对方选择接收机器", " · Recipient chooses a device"),
    }),
  );
  const section = el("section", "panel manage-panel manage-transfer-send");
  section.append(el("h2", "", t("发送文件", "Send files")));
  if (!targets.length) {
    section.append(
      el(
        "p",
        "manage-empty",
        t(
          "关联另一台设备或加入共同项目后，即可选择接收方。",
          "Link another device or join a shared project to choose a recipient.",
        ),
      ),
    );
    return section;
  }
  const recipients = new Map(
    targets.map((item) => [
      JSON.stringify([item.server, item.recipient_id, item.recipient_node]),
      item,
    ]),
  );
  const controls = el("div", "manage-transfer-controls");
  const targetField = el("div", "manage-form-field");
  const account = S.dash.user.id;
  const savedTarget =
    transferTarget?.account === account ? transferTarget.key : null;
  const target = field(
    targetField,
    t("发送给", "Recipient"),
    recipients.has(savedTarget) ? savedTarget : recipients.keys().next().value,
    [...recipients].map(([key, item]) => [key, item.label]),
  );
  target.onchange = () => {
    transferTarget = { account, key: target.value };
  };
  controls.append(
    targetField,
    button(
      t("选择文件并发送", "Choose file and send"),
      async () => {
        const selected = recipients.get(target.value);
        const result = await api("file-transfer-direct-send", {
          server: selected.server,
          ...(selected.recipient_node
            ? { to_node: selected.recipient_node }
            : { recipient: selected.recipient_id }),
        });
        if (result.selected) {
          notify(t("文件已开始发送。", "File transfer started."));
          await renderTransfers("action");
        }
      },
      "primary",
    ),
  );
  section.append(controls);
  return section;
}

async function renderTransfers(reason) {
  nav("transfers");
  if (transferPolling) {
    // An action must not be swallowed by an in-flight poll: queue one re-render.
    if (reason === "action") transferPending = true;
    return;
  }
  transferPolling = true;
  const content = $("content");
  // The marker lives in the DOM, so returning from another page always rebuilds.
  const mounted = !!content.querySelector("[data-transfer-root]");
  if (!mounted) {
    if (transferSnapshot) paintTransfers(content, transferSnapshot);
    else {
      content.replaceChildren(transfersHeading());
      content.append(
        el("div", "panel", t("正在读取传输状态…", "Loading transfer status…")),
      );
    }
  }
  try {
    const view = await api("file-transfer-dashboard");
    if (S.page !== "transfers") return;
    // Rebuilding identical DOM every three seconds is what made this page
    // flicker, so an unchanged snapshot keeps the page as it is.
    const signature = JSON.stringify(view);
    if (mounted && signature === transferSignature) return;
    if (
      mounted &&
      reason !== "action" &&
      content.querySelector(".manage-transfer-send select:focus")
    )
      return;
    transferSignature = signature;
    transferSnapshot = view;
    paintTransfers(content, view);
  } catch (error) {
    // A failed poll keeps the last good page; only a fresh mount has nothing to
    // keep and must show the failure.
    if (!mounted && !transferSnapshot) {
      content.replaceChildren(transfersHeading());
      content.append(el("div", "panel error", error.message));
    }
  } finally {
    transferPolling = false;
    if (transferPending) {
      transferPending = false;
      // Never pull the user back to this page from the queued re-render.
      if (S.page === "transfers") renderTransfers();
    }
  }
}

setInterval(() => {
  if (S.page === "transfers" && !document.hidden) renderTransfers();
}, 3000);

Object.assign(FoolshipDesktop.render, { renderTransfers });
