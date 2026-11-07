# 直连速传：现状与剩余工作

> 交接文档。新会话请先读 `README.md`、`docs/ARCHITECTURE.md`，再读本文。
> 最近发布：`v2.5.0`（直连速传已对用户生效）。

## 已定的设计决定

| 决定 | 理由 |
|---|---|
| 设备直连，中心只做信令 | Tailscale/WireGuard 已提供端到端加密；中心不碰字节，也就不需要应用层加密 |
| 发送方监听、接收方拉取 | 源文件就在发送方磁盘上，天然可续传，两侧都不留存副本 |
| 认证 = 设备 Ed25519 签名（传输 id + 时间戳 + 资源路径，5 分钟窗口） | 同项目其他成员无法取走文件；对服务运营者不是边界（它发布公钥），与既有信任模型一致 |
| 接收规则：同账户自动；跨账户首次需确认，确认后记住发送账户 | 与是否共同参与项目无关 |
| 默认接收目录 + 已信任发送人 → 自动接收；否则每次询问 | 常见路径零交互 |
| 不排队：对方不可达就失败 | 中心暂存正是要取消的行为 |

## 已经完成

| 文件 | 内容 |
|---|---|
| `src/agent_harness/direct_transfer.py` | `Sender`（Range 服务、签名校验、`set_recipient`）、`fetch`（`.part` 续传、大小校验）、`probe`（探连）、`local_tailnet_address` |
| `src/agent_harness/direct_signaling.py` | schema v6 `direct_transfers`：offer/respond/progress/visible/view；`to_node` 可空，接受时认领并写入 `claim_key` |
| `src/agent_harness/transfer_trust.py` | 接收方本机信任的发送账户（`transfer-trust.json`，上限 200） |
| `src/agent_harness/direct_send.py` | 桌面侧 `send`/`cancel`/`observe`；拿不到 tailnet 地址直接报错，不降级 |
| `pull_worker._consume_transfers` | 接收端消费 offer：信任+目录才自动，否则写 `pending-transfers.json`；下载回报进度 |
| `pull_worker.transfer_signing_key` | 接收端自己的 Ed25519 身份（公钥随轮询上报，存 `nodes/<node>.json`） |
| 中心 | 桌面 op `transfer-direct-offer/respond/list`；设备端点 `transfer-respond/progress` |
| 界面 | 发送按钮只调用 `file-transfer-direct-send`；同账户带 `to_node`，跨账户带 `recipient` |
| 旧通道 | 发送入口已全部改走直连；历史行带 `transport: "staged"` |

## 剩余工作（按顺序）

1. **速传页改数据源**（`assets/web/app/transfers.js`）
   - `file-transfer-dashboard` → 读 `transfer-direct-list`，旧 `staged` 行只读展示
   - `file-transfer-accept/reject` → `transfer-direct-respond`
   - 待确认列表读本机 `pending-transfers.json`
2. **接受时带上认领设备的公钥**：桌面替某台机器接受时，必须把该设备的 `transfer_public_key` 作为 `claim_key` 一并提交，否则发送方无法验证下载请求（当前只有设备自动接受时带）。
3. **Linux 接收端换下载器**：`headless_transfers.py` 的暂存接收改为 `direct_transfer.fetch`
4. **删除死代码**：`file_transfers.py`、`headless_transfers.py`、`account_backend` 里 6 个 `transfer-*` op、`broker` 的设备上传/下载端点、对应测试；保留 `file-transfer-receive-folder`

## 已知风险

- **Windows 防火墙**：首次监听会弹授权，需要引导文案
- **地址发现失败**：`tailscale ip` 不可用时必须报错，不允许回退到中心暂存
- **发送方休眠**：GB 级传输可能跨休眠，`fetch` 目前不自动重连重试，需要外部循环
- **并发**：每笔发送一个端口，需要上限（建议 4）
- **测试波动**：`tests/test_file_transfer_client.py` 在全量运行中偶发失败、单跑通过，原因未定位
