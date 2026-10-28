"use strict";

let transferPolling = false;

function transferStateLabel(item, job) {
  if (job?.state === "running") {
    const labels = {
      hashing: "正在核对文件",
      verifying_source: "正在确认源文件",
      uploading: "正在发送",
      downloading: "正在接收",
      verifying: "正在校验",
      local_queue: "等待本机传输队列",
    };
    const total = Number(job.total_bytes || item.size || 0),
      done = Number(job.completed_bytes || item.received_bytes || 0),
      percent = total
        ? " · " + Math.min(100, Math.floor((done / total) * 100)) + "%"
        : "";
    return (labels[job.phase] || "传输中") + percent;
  }
  if (job?.state === "paused") return "已暂停 · " + (job.error || "稍后可重试");
  const labels = {
    requested: "等待接收方确认",
    accepted: "已接受，等待发送方应用继续；离线时会在下次启动后恢复",
    uploading: "发送中",
    ready: "已送达服务，等待接收设备保存",
    completed: "已完成",
    rejected: "已拒绝",
    cancelled: "已取消",
    expired: "已过期",
  };
  return labels[item.state] || item.state;
}

async function renderTransfers() {
  nav("transfers");
  if (transferPolling) return;
  transferPolling = true;
  const content = $("content");
  content.replaceChildren(
    head("文件速传", "独立于项目和 Agent 的机器间大文件通道"),
  );
  const loading = el("div", "panel", "正在读取传输状态…");
  content.append(loading);
  try {
    const view = await api("file-transfer-dashboard");
    if (S.page !== "transfers") return;
    content.replaceChildren(
      head("文件速传", "独立于项目和 Agent 的机器间大文件通道"),
    );
    if (view.unavailable_services?.length)
      content.append(
        el(
          "div",
          "panel error",
          "部分协作服务暂时离线，对应联系人和传输恢复后会继续显示。",
        ),
      );
    const settings = el("section", "panel transfer-settings");
    settings.append(
      el("h2", "", "接收设置"),
      el(
        "p",
        "muted",
        view.settings?.receive_folder_configured
          ? "同一账户的其他机器发来文件时，会自动保存到本机默认接收目录。"
          : "请先设置本机默认接收目录；同账户机器之间传输无需逐次确认。",
      ),
      button(
        view.settings?.receive_folder_configured
          ? "更换默认接收目录"
          : "设置默认接收目录",
        async () => {
          const result = await api("file-transfer-receive-folder");
          if (result.configured) {
            notify("默认接收目录已保存，仅保存在本机。");
            await renderTransfers();
          }
        },
      ),
    );
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
      settings.append(el("h3", "", "其他机器的接收目录"));
      note(
        settings,
        "同账户传输和你批准给远程机器的文件会使用目标机器自己的接收目录。Linux 路径由 Linux 本机验证并保存。",
      );
      remoteDevices.forEach((device) => {
        const row = el("div", "setting-row");
        row.append(
          el(
            "span",
            "",
            device.name +
              " · " +
              serviceName(device.server) +
              (device.online ? "" : " · 离线"),
          ),
          button("设置接收目录", () => {
            modal("设置 " + device.name + " 的接收目录", (body, actions) => {
              const destination = field(body, "目标机器上的绝对路径", "");
              destination.placeholder =
                "例如 /home/user/Downloads/Harness 或 D:\\Transfers";
              note(
                body,
                "路径会随签名控制指令经过当前可信协作服务，并在目标机器处理后从服务记录中删除。目标机器必须在线，且该目录必须已经存在。",
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
                      notify(
                        "设置指令已发送；目标机器在线后会验证并保存目录。",
                      );
                    }
                  },
                  "primary",
                ),
              );
            });
          }),
        );
        settings.append(row);
      });
    }
    content.append(settings);

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
    const send = el("section", "panel");
    send.append(el("h2", "", "发送文件"));
    if (!targets.length) {
      empty(
        send,
        "当前没有可用目标。请先关联自己的另一台机器，或与对方共同加入一个协作项目。",
      );
    } else {
      const target = field(
        send,
        "发送给",
        "0",
        targets.map((item, index) => [String(index), item.label]),
      );
      note(
        send,
        "不同用户之间会先发送请求；对方选择保存位置并确认后才开始传输。文件通过双方共同使用的协作服务临时中转，不会加入项目；中转不是端到端加密，服务托管者可读取传输中的文件。",
      );
      send.append(
        button(
          "选择文件并发送",
          async () => {
            const selected = targets[Number(target.value)];
            const result = await api("file-transfer-send-select", {
              server: selected.server,
              recipient_id: selected.recipient_id,
              ...(selected.recipient_node
                ? { recipient_node: selected.recipient_node }
                : {}),
            });
            if (result.selected) {
              notify("文件已进入发送队列。大文件校验会在后台进行。");
              await renderTransfers();
            }
          },
          "primary",
        ),
      );
    }
    content.append(send);

    const history = el("section", "panel");
    history.append(el("h2", "", "传输记录"));
    const localJobs = view.jobs || [],
      jobs = new Map(
        localJobs.filter((j) => j.transfer_id).map((j) => [j.transfer_id, j]),
      );
    if (!(view.transfers || []).length && !localJobs.length)
      empty(history, "还没有文件传输记录。");
    localJobs
      .filter((job) => !job.transfer_id)
      .forEach((job) => {
        const row = el("div", "transfer-row"),
          info = el("div", "transfer-info");
        info.append(
          el("strong", "", "准备发送 · " + job.name),
          el("span", "muted small", fileSize(job.total_bytes || 0)),
          el(
            "span",
            "small",
            transferStateLabel({ size: job.total_bytes }, job),
          ),
        );
        row.append(info);
        history.append(row);
      });
    (view.transfers || []).forEach((item) => {
      const incoming = item.recipient_id === S.dash.user.id,
        job = jobs.get(item.id),
        row = el("div", "transfer-row"),
        info = el("div", "transfer-info");
      info.append(
        el("strong", "", (incoming ? "收到 · " : "发出 · ") + item.name),
        el(
          "span",
          "muted small",
          (item.peer_name || "对方设备") +
            " · " +
            fileSize(item.size) +
            " · " +
            date(item.created_at),
        ),
        el("span", "small", transferStateLabel(item, job)),
      );
      row.append(info);
      if (incoming && item.state === "requested") {
        let machines = (view.devices || []).filter(
          (device) =>
            device.server === item.server &&
            (!item.recipient_node || device.node === item.recipient_node),
        );
        const target = machines.length
          ? field(
              row,
              "接收到",
              "0",
              machines.map((device, index) => [
                String(index),
                device.name + (device.online ? "" : "（离线，恢复后自动接收）"),
              ]),
            )
          : null;
        row.append(
          button("拒绝", async () => {
            await api("file-transfer-reject", {
              server: item.server,
              id: item.id,
            });
            await renderTransfers();
          }),
          button(
            "确认接收",
            async () => {
              if (!target) throw new Error("当前账户没有可用的接收机器");
              const selected = machines[Number(target.value)];
              const result = await api("file-transfer-accept", {
                server: item.server,
                id: item.id,
                recipient_node: selected.node,
              });
              if (result.accepted)
                notify(
                  result.remote
                    ? "已确认；目标机器恢复在线后会自动接收。"
                    : "已确认接收；发送方在线时会开始传输。",
                );
              await renderTransfers();
            },
            "primary",
          ),
        );
      }
      const localNodes = new Set(
        (S.dash?.devices || [])
          .filter((device) => device.local)
          .map((device) => device.node || device.id),
      );
      if (
        incoming &&
        ["accepted", "ready"].includes(item.state) &&
        !job &&
        localNodes.has(item.recipient_node)
      )
        row.append(
          button(
            "选择位置并继续接收",
            async () => {
              const result = await api("file-transfer-accept", {
                server: item.server,
                id: item.id,
              });
              if (result.accepted) notify("接收位置已保存，将自动继续传输。");
              await renderTransfers();
            },
            "primary",
          ),
        );
      if (
        !["completed", "rejected", "cancelled", "expired"].includes(item.state)
      )
        row.append(
          button(
            "取消",
            async () => {
              await api("file-transfer-cancel", {
                server: item.server,
                id: item.id,
              });
              await renderTransfers();
            },
            "link",
          ),
        );
      history.append(row);
    });
    content.append(history);
  } catch (error) {
    loading.className = "panel error";
    loading.textContent = error.message;
  } finally {
    transferPolling = false;
  }
}

setInterval(() => {
  if (S.page === "transfers" && !document.hidden) renderTransfers();
}, 3000);

Object.assign(HarnessDesktop.render, { renderTransfers });
