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

## 设备接入（Mac mini 网关 + Docker）

不要把手机 ADB 暴露到公网。推荐办公网放一台 Mac mini 作为网关，手机 USB 连它；ECS 上的容器通过 Tailscale 访问 Mac mini 的 adb server（端口 5037）。

### Mac mini（网关）

```bash
# 1. 安装 platform-tools，版本与 ECS 保持一致（如 37.0.1）
mkdir -p ~/platform-tools && cd ~/platform-tools
curl -O https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
unzip platform-tools-latest-darwin.zip

# 2. 手机 USB 接入并授权调试
~/platform-tools/platform-tools/adb devices

# 3. 监听所有网卡的 adb server
adb kill-server
adb -a -P 5037 nodaemon server start

# 4. Tailscale
brew install tailscale && sudo brew services start tailscale
tailscale up
tailscale ip -4        # 例：100.x.x.10
```

- 关闭休眠：`sudo pmset -a sleep 0 disksleep 0`
- 防火墙只允许 Tailscale 网段（`100.64.0.0/10`）访问 5037
- 用 launchd 保活 adb server（示例见 `docs`/前文），避免 Mac 重启后失效

### ECS（Docker）

```bash
# 1. 下载 Linux 版 platform-tools（版本与 Mac mini 对齐）
curl -o /tmp/pt.zip https://dl.google.com/android/repository/platform-tools-latest-linux.zip
sudo unzip -q /tmp/pt.zip -d /opt
/opt/platform-tools/adb version

# 2. 确认 ECS 能访问 Mac mini 的 adb server
nc -vz 100.x.x.10 5037

# 3. deploy/.env
ADB_SERVER_SOCKET=tcp:100.x.x.10:5037
PLATFORM_TOOLS_DIR=/opt/platform-tools

# 4. 启动
docker compose -f deploy/docker-compose.prod.yml up -d --build

# 5. 容器内验证
docker compose -f deploy/docker-compose.prod.yml exec autoglm adb devices
curl -H "X-API-Key: $AUTOGLM_INGEST_TOKEN" http://127.0.0.1:8000/api/ingest/devices
```

如果容器访问不到 `100.x`，把 compose 改成 `network_mode: host` 并去掉 `ports`（文件里有注释）。此时服务监听宿主机 `127.0.0.1:8000`，阿里云安全组仍然不要开放 8000。

### 手机准备

1. 开发者选项 → USB 调试，首次连接在手机上点「允许」
2. 充电时保持唤醒；锁屏设为「无」或长超时（发送器不能解 PIN）
3. 关闭微信、ADBKeyboard 的电池优化
4. 小米/HyperOS 开启「USB 安装」，否则 ADBKeyboard 装不上
5. 微信保持登录，群名与后台配置一致

### 安全

- 阿里云安全组**不要**开放 5037 / 5555
- Mac mini 的 5037 只允许 Tailscale 网段访问
- 发送服务 8000 只绑 `127.0.0.1`，外部访问走 Caddy/Nginx + TLS + Basic Auth

> 如果不用 Tailscale，也可以在 Mac mini 上用 `autossh -R 5037:127.0.0.1:5037` 反向隧道到 ECS；此时 ECS 上设 `ADB_SERVER_SOCKET=tcp:127.0.0.1:5037`，容器需要 host 网络或把隧道绑到容器可达的地址。

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