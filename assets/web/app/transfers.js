"use strict";

let transferPolling = false;
let transferPending = false;
let transferSignature = "";

function transfersHeading() {
  const title = head("文件速传", "独立于项目和 Agent 的机器间大文件通道");
  title.dataset.transferRoot = "1";
  return title;
}

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
    content.replaceChildren(transfersHeading());
    content.append(el("div", "panel", "正在读取传输状态…"));
  }
  try {
    const view = await api("file-transfer-dashboard");
    if (S.page !== "transfers") return;
    // Rebuilding identical DOM every three seconds is what made this page
    // flicker, so an unchanged snapshot keeps the page as it is.
    const signature = JSON.stringify(view);
    if (mounted && signature === transferSignature) return;
    transferSignature = signature;
    content.replaceChildren(transfersHeading());
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
            await renderTransfers("action");
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

    const directRows = view.direct || [];
    if (directRows.length) {
      const direct = el("section", "panel");
      direct.append(el("h2", "", "直连传输"));
      note(
        direct,
        "文件由对方的机器直接传到这台机器，协作服务不接触内容。跨账户第一次来件需要你确认，确认后会记住该发送账户。",
      );
      const needsFolder = !view.settings?.receive_folder_configured;
      directRows.forEach((item) => {
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
        direct.append(row);
      });
      content.append(direct);
    }

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
        "对方确认后，文件从你的机器直接传到对方的机器；协作服务只转交“谁发给谁、大小、地址”，不接触文件内容。",
      );
      send.append(
        button(
          "选择文件并发送",
          async () => {
            const selected = targets[Number(target.value)];
            // A known device of my own account transfers directly: the bytes go
            // over the tailnet and the collaboration service never holds them.
            const result = await api("file-transfer-direct-send", {
              server: selected.server,
              ...(selected.recipient_node
                ? { to_node: selected.recipient_node }
                : { recipient: selected.recipient_id }),
            });
            if (result.selected) {
              notify("文件已进入发送队列。大文件校验会在后台进行。");
              await renderTransfers("action");
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
            await renderTransfers("action");
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
              await renderTransfers("action");
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
              await renderTransfers("action");
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
              await renderTransfers("action");
            },
            "link",
          ),
        );
      history.append(row);
    });
    content.append(history);
  } catch (error) {
    // A failed poll keeps the last good page; only a fresh mount has nothing to
    // keep and must show the failure.
    if (!mounted) {
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

Object.assign(HarnessDesktop.render, { renderTransfers });
