# AutoGLM-GUI 生产部署

面向「内部系统推送消息 → 服务在时间窗内自动离散发送 → Webhook 回执」这一形态。
GUI 只用于查看状态和人工重触发。

## 架构

```
内部系统 --HTTP POST /api/ingest/push-batches--> 服务
                                            |
                                            +-> push.db（批次/明细/重试状态）
                                            +-> 调度器在时间窗内离散入队
                                            +-> TaskManager（同设备串行、跨设备并行）
                                            +-> 确定性微信发送器（ADB + 文本注入 + 视觉校验）
                                            +-> Webhook 回调 + 查询接口兜底
```

## 发送执行方式

推送任务使用确定性发送器，不再依赖通用 Agent 自由发挥：

1. 强制重启微信，回到会话列表（视觉校验底部四个 tab）
2. 精确搜索群名，打开第一条结果
3. 视觉校验顶部标题包含目标群名，不一致直接失败、不发送
4. 清空该会话的历史草稿，输入 `@`，点击「所有人」，视觉校验提及已选中
5. 通过 ADB Keyboard 以 base64 注入正文，换行/标点原样保留
6. 点击发送，确认输入框已清空；否则视为失败

注意：

- 需要服务配置可用的视觉模型（`AUTOGLM_BASE_URL/MODEL_NAME/API_KEY`），视觉校验不可用时该条失败而不是盲发。
- 目前只把 `@所有人` 处理为真实提及；正文里出现的其他 `@某人` 是普通文本，不会触发提醒。
- 坐标按 1080×2400、微信 8.0.7x 校准；换机型或大版本需要更新 `AutoGLM_GUI/push/wechat_sender.py` 里的 `WeChatProfile`。
- 每条消息发送前会重启微信，因此手机不要同时用于人工聊天。

## 前置条件

- Linux 云服务器，Docker + Docker Compose v2
- 一个域名（Caddy 自动签发 TLS）
- 手机与云服务器网络互通：**推荐 WireGuard / Tailscale 隧道**，不要在公网暴露 ADB 5555 端口
- 内部系统能访问本服务的 HTTPS 域名

## 配置

```bash
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，填入模型配置、ingest token、webhook secret、域名
```

关键环境变量：

| 变量 | 说明 |
|---|---|
| `AUTOGLM_BASE_URL` | 模型 API 地址，例如 `https://open.bigmodel.cn/api/paas/v4` |
| `AUTOGLM_MODEL_NAME` | 模型名，例如 `autoglm-phone` |
| `AUTOGLM_API_KEY` | 模型 API Key |
| `AUTOGLM_INGEST_TOKEN` | 内部系统调用 ingest 接口的 `X-API-Key`；**未设置时 ingest 接口返回 503** |
| `AUTOGLM_WEBHOOK_SECRET` | Webhook HMAC 密钥 |
| `AUTOGLM_WEBHOOK_ALLOWED_HOSTS` | 可选，逗号分隔的回调域名白名单（防 SSRF） |
| `AUTOGLM_PUSH_TIMEZONE` | 默认时间窗的时区，默认 `Asia/Shanghai` |
| `AUTOGLM_CORS_ORIGINS` | 允许的浏览器来源；生产填你的域名 |
| `TZ` | 容器时区，建议与 `AUTOGLM_PUSH_TIMEZONE` 一致 |

## 启动

```bash
cd deploy
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f autoglm
```

然后配置 Caddy（替换域名和密码哈希）：

```bash
caddy hash-password --plaintext 'your-strong-password'
caddy run --config deploy/Caddyfile
```

打开 `https://app.example.com`，用反向代理的 Basic Auth 登录。

## 设备接入

1. 在云服务器与手机所在局域网之间建立 WireGuard/Tailscale 隧道。
2. 手机开启「无线调试」，在服务器上执行：
   ```bash
   adb connect <手机内网IP>:<端口>
   adb devices
   ```
3. 设备出现在 `GET /api/ingest/devices`（带 token）即接入成功。
4. 首次使用需要在手机上允许「USB 安装」并安装 ADB Keyboard（服务会自动尝试）。

> 重启服务器后 adb TCP 连接可能丢失，需要重新 `adb connect`。建议把该命令写入
> VPN 启动脚本，或在设备恢复后重新连接。

## 数据与备份

所有运行状态都在两个卷里：

| 卷 | 容器路径 | 内容 |
|---|---|---|
| `autoglm_config` | `/root/.config/autoglm` | `tasks.db`、`push.db`、模型配置、设备分组 |
| `autoglm_logs` | `/app/logs` | 应用日志、trace JSONL、运行回放 |

备份示例：

```bash
docker run --rm -v deploy_autoglm_config:/data -v "$PWD":/backup alpine \
  tar czf /backup/autoglm-config-$(date +%F).tgz -C /data .
```

`push.db` 中已完成且超过 30 天的批次会在 dispatcher 启动时自动清理。

## 运维说明

- **单副本**：设备状态、Agent 实例、任务 worker 都在进程内存，SQLite 也不适合多写者。不要横向扩容。
- **升级**：`docker compose -f docker-compose.prod.yml up -d --build`；升级前备份 config 卷。
- **健康检查**：`/api/health` 只表示进程存活，不校验 ADB/模型连通性。
- **日志**：`/app/logs/autoglm_*.log`、`errors_*.log`、`trace_*.jsonl`。
- **Web terminal**：默认关闭，生产环境不要开启。

## 安全清单

- [ ] ingest token 足够长且只分发给内部系统
- [ ] webhook secret 已配置，回调端校验 `X-AutoGLM-Signature`
- [ ] 反向代理启用 HTTPS + Basic Auth（或换成 OIDC）
- [ ] 不向公网暴露 ADB 端口
- [ ] `AUTOGLM_CORS_ORIGINS` 收敛到你的域名
- [ ] 定期备份 config 卷

内部系统对接契约见 [INTEGRATION.md](./INTEGRATION.md)。