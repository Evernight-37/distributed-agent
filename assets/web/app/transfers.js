"use strict";

let transferPolling = false;
let transferPending = false;
let transferSignature = "";
let transferSnapshot = null;

function transfersHeading() {
  const title = head("文件速传", "独立于项目和 Agent 的机器间大文件通道");
  title.dataset.transferRoot = "1";
  return title;
}

function directStateLabel(item) {
  const labels = {
    queued: item.incoming ? "等待你确认" : "等待对方确认",
    accepted: "已确认，等待对方就绪",
    running: "传输中",
    done: "已完成",
    failed: "失败" + (item.error ? " · " + item.error : ""),
    rejected: "已拒绝",
  };
  const percent =
    item.state === "running" && item.size && item.transferred
      ? " · " +
        Math.min(100, Math.floor((item.transferred / item.size) * 100)) +
        "%"
      : "";
  return (labels[item.state] || item.state) + percent;
}

function transferSettings(view) {
  const section = el("section", "panel transfer-settings");
  const configured = !!view.settings?.receive_folder_configured;
  section.append(el("h2", "", "接收设置"));
  note(
    section,
    configured
      ? "同账户的机器发来文件时自动收下，保存到本机默认接收目录；其他账户第一次发来需要你在这里确认一次，之后记住该账户。"
      : "还没有设置本机默认接收目录：即使同账户的机器发来文件，也只会停在列表里等确认，不会自动落地。",
  );
  const local = el("div", "setting-row");
  local.append(
    el("div", "", "本机默认接收目录" + (configured ? "" : " · 未设置")),
    button(configured ? "更换目录" : "设置目录", async () => {
      const result = await api("file-transfer-receive-folder");
      if (result.configured) {
        notify("默认接收目录已保存，仅保存在本机。");
        await renderTransfers("action");
      }
    }),
  );
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
    section.append(el("h3", "", "其他机器"));
    note(
      section,
      "账户里的其他机器收到文件时，各自保存到自己的接收目录。这里只转交设置，目录不存在时由目标机器创建。",
    );
    remoteDevices.forEach((device) => {
      const row = el("div", "setting-row");
      const label = el("div", "", device.name);
      label.append(
        el(
          "span",
          "muted small",
          " · " +
            serviceName(device.server) +
            (device.online ? " · 在线" : " · 离线"),
        ),
      );
      row.append(
        label,
        button("设置接收目录", () => {
          modal("设置 " + device.name + " 的接收目录", (body, actions) => {
            const destination = field(body, "目标机器上的绝对路径", "");
            destination.placeholder =
              "例如 /home/user/Downloads/Foolship 或 D:\\Transfers";
            note(
              body,
              "目标机器在线时这条指令会被送达并保存；目录不存在会自动创建。路径随签名控制指令经过当前可信协作服务，目标机器处理后从服务记录中删除。",
            );
            actions.append(
              button("取消", closeModal),
              button(
                "发送设置指令",
                async () => {
                  const result = await api("file-transfer-receive-folder", {
                    server: device.server,
                    node: device.node,
                    path: destination.value.trim(),
                  });
                  if (result.pending) {
                    closeModal();
                    notify("设置指令已发送；目标机器在线后会创建并保存目录。");
                  }
                },
                "primary",
              ),
            );
          });
        }),
      );
      section.append(row);
    });
  }
  return section;
}

function paintTransfers(content, view) {
  content.replaceChildren(transfersHeading());
  if (view.unavailable_services?.length)
    content.append(
      el(
        "div",
        "panel error",
        "部分协作服务暂时离线，对应联系人和传输恢复后会继续显示。",
      ),
    );
  content.append(transferSettings(view));
  const direct = transferDirect(view);
  if (direct) content.append(direct);
  content.append(transferSend(view));
}

function transferDirect(view) {
  const rows = view.direct || [];
  if (!rows.length) return null;
  const section = el("section", "panel");
  section.append(el("h2", "", "直连传输"));
  note(
    section,
    "文件由对方的机器直接传到这台机器，协作服务不接触内容。跨账户第一次来件需要你确认，确认后会记住该发送账户。",
  );
  const needsFolder = !view.settings?.receive_folder_configured;
  rows.forEach((item) => {
    const row = el("div", "transfer-row"),
      info = el("div", "transfer-info");
    info.append(
      el("strong", "", (item.incoming ? "收到 · " : "发出 · ") + item.name),
      el(
        "span",
        "muted small",
        (item.peer_name || "对方设备") +
          " · " +
          fileSize(item.size) +
          " · " +
          date(item.created_at),
      ),
      el("span", "small", directStateLabel(item)),
    );
    row.append(info);
    if (item.incoming && item.state === "queued") {
      row.append(
        button("拒绝", async () => {
          await api("file-transfer-direct-respond", {
            server: item.server,
            id: item.id,
            accept: false,
          });
          await renderTransfers("action");
        }),
        button(
          "确认接收",
          async () => {
            if (needsFolder) throw new Error("请先设置默认接收目录");
            await api("file-transfer-direct-respond", {
              server: item.server,
              id: item.id,
              accept: true,
            });
            notify("已确认；文件会直接传到本机接收目录。");
            await renderTransfers("action");
          },
          "primary",
        ),
      );
    }
    section.append(row);
  });
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
        label: "我的机器 · " + device.name,
      });
  });
  (view.contacts || []).forEach((person) =>
    targets.push({
      server: person.server,
      recipient_id: person.id,
      recipient_node: null,
      label: person.username + " · 由对方选择接收机器",
    }),
  );
  const section = el("section", "panel");
  section.append(el("h2", "", "发送文件"));
  if (!targets.length) {
    empty(
      section,
      "当前没有可用目标。请先关联自己的另一台机器，或与对方共同加入一个协作项目。",
    );
    return section;
  }
  const target = field(
    section,
    "发送给",
    "0",
    targets.map((item, index) => [String(index), item.label]),
  );
  note(
    section,
    "对方确认后，文件从你的机器直接传到对方的机器；协作服务只转交“谁发给谁、大小、地址”，不接触文件内容。",
  );
  section.append(
    button(
      "选择文件并发送",
      async () => {
        const selected = targets[Number(target.value)];
        const result = await api("file-transfer-direct-send", {
          server: selected.server,
          ...(selected.recipient_node
            ? { to_node: selected.recipient_node }
            : { recipient: selected.recipient_id }),
        });
        if (result.selected) {
          notify("文件已开始发送。");
          await renderTransfers("action");
        }
      },
      "primary",
    ),
  );
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
      content.append(el("div", "panel", "正在读取传输状态…"));
    }
  }
  try {
    const view = await api("file-transfer-dashboard");
    if (S.page !== "transfers") return;
    // Rebuilding identical DOM every three seconds is what made this page
    // flicker, so an unchanged snapshot keeps the page as it is.
    const signature = JSON.stringify(view);
    if (mounted && signature === transferSignature) return;
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
