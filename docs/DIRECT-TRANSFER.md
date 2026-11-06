# 直连速传：设计决定与剩余接线

传输层已完成并在 `src/agent_harness/direct_transfer.py`，测试在 `tests/test_direct_transfer.py`。
本文记录已定的决定与接线步骤，避免下一次改动重新讨论。

## 已定的决定

| 决定 | 理由 |
|---|---|
| 设备直连，中心只做信令 | Tailscale/WireGuard 已提供端到端加密；中心不碰字节，也就不需要再写一层应用加密 |
| 发送方监听、接收方拉取 | 源文件就在发送方磁盘上，天然可续传，任何一侧都不留存副本 |
| 认证用设备 Ed25519 签名（传输 id + 时间戳 + 资源路径，5 分钟窗口） | 防止同项目其他成员取走文件；对服务运营者不是边界（它发布设备公钥），这与项目既有的信任模型一致 |
| 续传用 HTTP Range，接收端 `.part` + 完成后原子改名 | 几 GB 的文件必须能跨断线/休眠继续 |
| 不排队：对方不可达就明确失败 | 暂存在中心正是本次要取消的行为 |
| 默认接收位置存在时自动接收，否则人工确认 | 常见路径零交互，陌生来源仍然可控 |

## 剩余接线（按顺序）

1. **中心信令表**（`storage.py` 增加 `direct_transfers`，schema v6）
   列：`id, from_node, to_node, size, name, address, state, created_at, updated_at`。
   `state ∈ queued/accepted/running/done/failed/rejected`。
   只存元数据与端点地址，绝不存内容。

2. **中心接口**（`file_transfers.py` 或新建 `direct_signaling.py`）
   - `offer`：发送方登记一行，返回 id
   - `accept` / `reject`：接收方决定
   - `progress`：任一方回报已传字节，供 UI 显示
   - 需要把 `direct_transfers` 挂进 `Broker._poll_snapshot` 的返回值，接收端轮询即可看到新 offer

3. **发送端**（`desktop_actions.py`）
   - `file-transfer-send` 改为：取目标设备公钥 → `direct_transfer.Sender(...).start()` → 取本机 tailnet 地址 → `offer` 登记 → 等接收方 accept/progress → 完成后 `stop()` 并清理
   - tailnet 地址：优先 `TAILSCALE_IP` 环境变量，其次解析 `tailscale ip -4`；取不到就明确失败，不要退化成"上传到中心"

4. **接收端**（`pull_worker.cycle`）
   - 读到新 offer → `file_transfer-settings.json` 有 `receive_folder` 且允许自动接收 → `direct_transfer.probe` 探连 → `fetch`
   - 无默认位置 → 记为待确认，由本地控制台/桌面 UI 决定
   - 状态与进度写回中心，失败保留 `.part` 以便续传

5. **删除旧暂存路径**
   - `file_transfers.py` 中 `_part`、分块上传、TTL 清理全部移除
   - `headless_transfers.py` 改为只处理信令
   - 迁移：旧的 `file_transfers` 行在读到时标记为 `legacy-staged`，提供"重新发送"而不尝试搬运历史字节

6. **UI**
   - 发送方：选文件 → 选目标设备 → 显示"探连中/传输中/已完成"
   - 接收方：默认位置时零交互；否则弹"是否接收"（含来源设备、大小、文件名）
   - 两处都显示速率与剩余时间，支持取消（取消即关闭连接，`.part` 保留）

## 当前风险点

- **Windows 入站防火墙**：第一次监听会弹授权；需要一个引导说明，否则用户会以为功能坏了。
- **地址发现**：如果 `tailscale ip` 不可用，直连无法建立；这时应直接报错，不要回退到中心暂存。
- **发送方休眠**：GB 级传输可能跨越休眠；续传已支持，但需要重连重试（当前 `fetch` 单次调用，不做自动重试）。
- **并发**：同一台机器同时对外发送多个文件时，每个 Sender 一个端口；需要上限（建议 4）。
