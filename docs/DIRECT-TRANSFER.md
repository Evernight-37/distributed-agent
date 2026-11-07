# 直连速传：现状与剩余工作

> 交接文档。新会话请先读 `README.md`、`docs/ARCHITECTURE.md`，再读本文。
> 最近发布：`v2.6.0`（桌面可以作为接收方；直连路径收发两个方向都已接通）。
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
| `src/agent_harness/direct_transfer.py` | `Sender`（Range 服务、签名校验、`set_recipient`、未认领 409、`X-Harness-Mtime`）、`pull`（probe + 续传 + 等待认领，两端共用一个循环）、`fetch`（`.part` + 出处标记续传、大小与 sha256 校验）、`probe`、`local_tailnet_address` |
| `src/agent_harness/direct_signaling.py` | schema v6 `direct_transfers`；`offer` 接受发送方编号（格式校验 + 唯一性）；respond/progress/visible/view |
| `src/agent_harness/transfer_trust.py` | 接收方本机信任的发送账户（`transfer-trust.json`，上限 200） |
| `src/agent_harness/direct_send.py` | 桌面侧 `send`/`cancel`/`observe`；发送时把编号交给中心；独立后台 watcher 学习认领公钥，不再依赖速传页渲染 |
| `pull_worker._consume_transfers` | 消费 offer：接受 `to_node` 为空的账户寻址（排除自己发出的行）；信任+目录才自动，否则写 `pending-transfers.json`；没配置目录时落到接收端自己的 `received-files` |
| `pull_worker._pull_direct` | 认领后重试（409 视为等待），按 meta 的 size/mtime/sha256 校验下载结果 |
| `pull_worker.transfer_signing_key` | 接收端自己的 Ed25519 身份（公钥随轮询上报，存 `nodes/<node>.json`） |
| `src/agent_harness/direct_receive.py` | 桌面接收方：自己的 Ed25519 身份（`transfer-key.json`，公钥经 `transfer-key-publish` 上报）、`incoming`/`outgoing`、自动与手动接受、后台拉取与进度上报、应用打开期间每 4 秒消费一次 |
| 中心 | op `transfer-direct-offer/respond/list/progress/key-publish`；respond 透传 `node` 与 `claim_key`；`transfer-peers` 带上各设备的 `transfer_public_key`；设备端点 `transfer-respond/progress` |
| 界面 | `file-transfer-dashboard` 返回 `direct` 列表（收/发两个方向），速传页新增"直连传输"区块；确认/拒绝走 `file-transfer-direct-respond` |
| 旧暂存通道 | 已整体删除：`file_transfers.py`、`file_transfer_client.py`、`headless_transfers.py`、设备端上传/下载端点、面板分片接口，以及 5 个桌面动作。`transfer-peers` 的名单查询已挪进 `direct_signaling.peers`；接收目录设置由 `direct_receive` 独占 |

## 剩余工作（按顺序）

1. **双机端到端验证**：本机测试覆盖不到 tailnet 连通性、真实中心与两台机器的时间窗；
   桌面接收路径目前只有本机 socket + 假中心的测试
2. **数据库里的旧表**：`file_transfers` / `file_transfer_tokens` 已无代码读写，但 `SCHEMA_V4`
   仍在建它们，`_v4_layout_current` 也仍以它们判断布局。要清理得加一次迁移，收益不大，暂时留着

## 已知风险

- **Windows 防火墙**：首次监听会弹授权，需要引导文案
- **地址发现失败**：`tailscale ip` 不可用时必须报错，不允许回退到中心暂存
- **同一 tailnet**：双方设备必须在同一 tailnet（或互相 share 了节点）才能直连；代码不检查，
  跨 tailnet 只会拨不通，而按设计中心不中转。"地址发现失败"只覆盖了没装 Tailscale 的情况
- **发送方休眠**：GB 级传输可能跨休眠。认领阶段现在会重试（约 12 秒），传输中断仍不重连，
  需要外部循环
- **并发**：每笔发送一个端口，`SENDERS` 无上限，也没有兜底回收（只在 watcher 看到终态时关闭）；
  接收端同理，`_INFLIGHT` 只在传输结束时清理。建议上限 4
- **接收方只在应用打开时工作**：桌面 watcher 随进程存活；退出应用期间来的传输停在 `queued`
- **测试波动**：`tests/test_file_transfer_client.py` 在全量运行中偶发失败、单跑通过，原因未定位（本次未复现）
