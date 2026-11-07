# 直连速传：现状与剩余工作

> 交接文档。新会话请先读 `README.md`、`docs/ARCHITECTURE.md`，再读本文。
> 最近发布：`v2.5.1`（已修复下面这条，端到端可用）。
>
> **2026-11-07 复核更正**：v2.5.0 发布时直连传输端到端不可用。发送方按自己生成的
> 编号建 URL 与签名，中心另生成一个编号给接收方，两侧签名永远不符，每一笔传输都
> 在第一条请求上被 403 拒绝（同账户、跨账户都一样）。已修复，见"已经完成"。

## 已定的设计决定

| 决定 | 理由 |
|---|---|
| 设备直连，中心只做信令 | Tailscale/WireGuard 已提供端到端加密；中心不碰字节，也就不需要应用层加密 |
| 发送方监听、接收方拉取 | 源文件就在发送方磁盘上，天然可续传，两侧都不留存副本 |
| 传输编号由发送方生成，中心沿用 | 发送方已按该编号建好 URL 与签名；中心另发一个编号，签名必然对不上 |
| 认证 = 设备 Ed25519 签名（传输 id + 时间戳 + 资源路径，5 分钟窗口） | 同项目其他成员无法取走文件；对服务运营者不是边界（它发布公钥），与既有信任模型一致 |
| 未认领时返回 409，不是 403 | 发送方还没拿到认领设备的公钥，谁也验证不了；这是"稍后重试"，不是"拒绝" |
| 续传只接续同一版本（size + mtime + sha256 一致才续） | 拼接两个版本会得到大小正确、内容错误的文件，且完全静默 |
| sha256 只在直连通道上比对，不进信令 | 内容指纹不交给中心，维持"中心看不到内容"这条边界 |
| 接收规则：同账户自动；跨账户首次需确认，确认后记住发送账户 | 与是否共同参与项目无关 |
| 默认接收目录 + 已信任发送人 → 自动接收；否则每次询问 | 常见路径零交互 |
| 不排队：对方不可达就失败 | 中心暂存正是要取消的行为 |

## 已经完成

| 文件 | 内容 |
|---|---|
| `src/agent_harness/direct_transfer.py` | `Sender`（Range 服务、签名校验、`set_recipient`、未认领 409、`X-Harness-Mtime`）、`fetch`（`.part` + 出处标记续传、大小与 sha256 校验、409 → `TransferPending`）、`probe`、`local_tailnet_address` |
| `src/agent_harness/direct_signaling.py` | schema v6 `direct_transfers`；`offer` 接受发送方编号（格式校验 + 唯一性）；respond/progress/visible/view |
| `src/agent_harness/transfer_trust.py` | 接收方本机信任的发送账户（`transfer-trust.json`，上限 200） |
| `src/agent_harness/direct_send.py` | 桌面侧 `send`/`cancel`/`observe`；发送时把编号交给中心；独立后台 watcher 学习认领公钥，不再依赖速传页渲染 |
| `pull_worker._consume_transfers` | 消费 offer：接受 `to_node` 为空的账户寻址（排除自己发出的行）；信任+目录才自动，否则写 `pending-transfers.json` |
| `pull_worker._pull_direct` | 认领后重试（409 视为等待），按 meta 的 size/mtime/sha256 校验下载结果 |
| `pull_worker.transfer_signing_key` | 接收端自己的 Ed25519 身份（公钥随轮询上报，存 `nodes/<node>.json`） |
| 中心 | 桌面 op `transfer-direct-offer/respond/list`；respond 透传 `node` 与 `claim_key`；设备端点 `transfer-respond/progress` |
| 界面 | 发送按钮只调用 `file-transfer-direct-send`；同账户带 `to_node`，跨账户带 `recipient`；旧通道入口已移除，历史行带 `transport: "staged"` |

## 剩余工作（按顺序）

1. **速传页改数据源 + 桌面接收端**（`assets/web/app/transfers.js`、`desktop_actions.py`）
   - `file-transfer-dashboard` → 读 `transfer-direct-list`，旧 `staged` 行只读展示
   - `file-transfer-accept/reject` → `transfer-direct-respond`
   - 待确认列表来自 `transfer-direct-list` 中指向本机（`to_node` 为空或等于本机）的行；
     注意 `pending-transfers.json` 只由 headless 接收端写，桌面不读它
   - 桌面目前没有任何消费 direct offer 的循环，接收侧在桌面上仍是旧通道
2. **桌面接受时带上认领设备的公钥**：服务端已透传 `node`/`claim_key`，客户端与页面还缺；
   headless 设备自动接受时已带
3. **删除死代码**：`file_transfers.py`、`headless_transfers.py` 及其调用点、
   `account_backend` 里 6 个 `transfer-*` op、`broker` 的设备上传/下载端点、对应测试；
   保留 `file-transfer-receive-folder`
   （原先单列的"Linux 接收端换下载器"已由 `_pull_direct` 完成，本条只剩删除）
4. **双机端到端验证**：本机测试覆盖不到 tailnet 连通性、真实中心与两台机器的时间窗

## 已知风险

- **Windows 防火墙**：首次监听会弹授权，需要引导文案
- **地址发现失败**：`tailscale ip` 不可用时必须报错，不允许回退到中心暂存
- **同一 tailnet**：双方设备必须在同一 tailnet（或互相 share 了节点）才能直连；代码不检查，
  跨 tailnet 只会拨不通，而按设计中心不中转。"地址发现失败"只覆盖了没装 Tailscale 的情况
- **发送方休眠**：GB 级传输可能跨休眠。认领阶段现在会重试（约 12 秒），传输中断仍不重连，
  需要外部循环
- **并发**：每笔发送一个端口，`SENDERS` 无上限，也没有兜底回收（只在 watcher 看到终态时关闭）；
  建议上限 4
- **测试波动**：`tests/test_file_transfer_client.py` 在全量运行中偶发失败、单跑通过，原因未定位（本次未复现）
