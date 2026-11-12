# 内部系统对接契约（初版）

> 这是初版契约，字段和路径后续可能随内部系统最终方案调整。
> 服务端把对接逻辑集中在 `AutoGLM_GUI/push/ingest_adapter.py` 和
> `AutoGLM_GUI/api/push_ingest.py`，调整协议时只需改这两处。

## 1. 鉴权

所有 `/api/ingest/*` 接口都需要请求头：

```
X-API-Key: <AUTOGLM_INGEST_TOKEN>
```

- 服务端未配置 `AUTOGLM_INGEST_TOKEN` 时返回 `503`。
- Key 不匹配返回 `401`。

## 2. 查询可用设备

```http
GET /api/ingest/devices
X-API-Key: ...
```

返回：

```json
[
  {
    "serial": "56fe42860624",
    "name": "设备一",
    "primary_device_id": "192.168.2.98:42851",
    "online": true,
    "connection_type": "wifi"
  }
]
```

内部系统使用 `serial` 作为 `device_serial` 即可；`online=false` 的设备仍然可以接收推送，
服务会在时间窗内等待它上线。

## 3. 推送一个批次

```http
POST /api/ingest/push-batches
Content-Type: application/json
X-API-Key: ...
```

```json
{
  "external_batch_id": "crm-2026-11-12-001",
  "callback_url": "https://internal.example.com/hooks/autoglm",
  "window_start": "2026-11-12T09:00:00+08:00",
  "window_end": "2026-11-12T11:00:00+08:00",
  "items": [
    {
      "external_item_id": "group-1",
      "device_serial": "56fe42860624",
      "group_name": "26.11.12小快灵测试一群",
      "message": "一群：【临床--签协议 开发票】还剩20天完成。\n‼️基金会要求 12月1日前完成全部发票递交，并于12月10日前完成付款\n尚未完成的人数：东区12人、南区25人、西区1人、北区17人、中区17人。\n\n*如药师担任药店法人/企业负责人的请提前告知@陈丽，我们会联系基金会出具盖章版本协议帮助老师开具发票，申请盖章件需要1-2周。",
      "mention_all": true
    },
    {
      "external_item_id": "group-2",
      "device_serial": "56fe42860624",
      "group_name": "26.11.12小快灵测试二群",
      "message": "二群：【临床--签协议 开发票】还剩20天完成。\n……"
    }
  ]
}
```

### 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `external_batch_id` | 是 | 幂等键，全局唯一 |
| `callback_url` | 否 | 本批次的 Webhook 地址；缺省用服务端默认地址 |
| `window_start` / `window_end` | 否 | ISO 8601 且必须带时区；两者同传。缺省用服务端默认时间窗 |
| `items` | 是 | 1 ~ `max_items_per_batch`（默认 500） |
| `items[].external_item_id` | 是 | 批次内唯一，用于状态回查和重触发 |
| `items[].device_serial` | 是 | 必须是服务已知设备 |
| `items[].group_name` | 是 | 微信群名，精确匹配 |
| `items[].message` | 是 | 原文，最多 10000 字符，**换行会原样保留** |
| `items[].mention_all` | 否 | 默认 `true`，先 @所有人 再输入内容 |
| `items[].send_after` / `send_before` | 否 | 单条消息的额外时间范围，覆盖批次窗口 |

### 响应

首次接收：`202 Accepted`

```json
{
  "batch_id": "0f0c...",
  "external_batch_id": "crm-2026-11-12-001",
  "status": "pending",
  "item_count": 2,
  "created": true,
  "items": [
    {
      "item_id": "a1b2...",
      "external_item_id": "group-1",
      "device_serial": "56fe42860624",
      "group_name": "26.11.12小快灵测试一群",
      "scheduled_at": "2026-11-12T01:37:12+00:00",
      "window_end": "2026-11-12T03:00:00+00:00"
    }
  ]
}
```

- 同一 `external_batch_id` + 相同 items / callback 重复推送：`200 OK`，`created=false`，即使时间窗不同也不会重复发送。
- 同一 `external_batch_id` 但 items / callback 不同：`409 Conflict`。
- 要重发同一批内容，必须使用新的 `external_batch_id`；管理端「重新推送」会追加随机后缀生成新批次。
- 每条消息的 `scheduled_at` 是服务在窗口内随机离散安排的时间（同一设备保持最小间隔）。

### 联调（只校验、不落库）

```http
POST /api/ingest/push-batches/dry-run
```

请求体同上，返回 `scheduled_at` 计划，但不会创建批次，也不会发送。

## 4. 查询批次状态

```http
GET /api/ingest/push-batches/{external_batch_id}
X-API-Key: ...
```

返回批次和全部明细：

```json
{
  "id": "0f0c...",
  "external_batch_id": "crm-2026-11-12-001",
  "status": "partial",
  "item_count": 2,
  "success_count": 1,
  "failure_count": 1,
  "items": [
    {
      "external_item_id": "group-1",
      "device_serial": "56fe42860624",
      "group_name": "26.11.12小快灵测试一群",
      "status": "succeeded",
      "attempts": 1,
      "task_id": "f3a1...",
      "last_error": null,
      "scheduled_at": "2026-11-12T01:37:12+00:00",
      "finished_at": "2026-11-12T01:39:03+00:00"
    }
  ]
}
```

批次状态：`pending` / `running` / `completed` / `partial` / `failed` / `cancelled`。
明细状态：`pending` / `dispatching` / `queued` / `running` / `succeeded` / `failed` / `cancelled`。

## 5. Webhook 回执

服务在每条消息结束、以及整批结束时调用 `callback_url`。

### 每条消息

```json
{
  "event": "push.item.completed",
  "batch_id": "0f0c...",
  "external_batch_id": "crm-2026-11-12-001",
  "item_id": "a1b2...",
  "external_item_id": "group-1",
  "device_serial": "56fe42860624",
  "group_name": "26.11.12小快灵测试一群",
  "status": "succeeded",
  "error": null,
  "task_id": "f3a1...",
  "finished_at": "2026-11-12T01:39:03+00:00"
}
```

### 整批完成

```json
{
  "event": "push.batch.completed",
  "batch_id": "0f0c...",
  "external_batch_id": "crm-2026-11-12-001",
  "status": "partial",
  "success_count": 1,
  "failure_count": 1,
  "cancelled_count": 0,
  "total_count": 2,
  "finished_at": "2026-11-12T01:40:11+00:00"
}
```

### 签名校验

请求头：

```
X-AutoGLM-Event: push.item.completed
X-AutoGLM-Signature: sha256=<hex>
```

签名是 `HMAC-SHA256(AUTOGLM_WEBHOOK_SECRET, 原始请求体)`。

Python：

```python
import hashlib, hmac

expected = "sha256=" + hmac.new(
    secret.encode(), raw_body, hashlib.sha256
).hexdigest()
assert hmac.compare_digest(expected, headers["X-AutoGLM-Signature"])
```

Node：

```js
const crypto = require('crypto');
const expected =
  'sha256=' +
  crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
```

### 重试

- 2xx 视为成功；其他状态码或网络错误会重试。
- 退避：1s、5s、30s、120s、600s，共 5 次；仍失败则标记为 `failed`，可在 GUI 查看。
- 回调地址仅允许 http/https；配置 `AUTOGLM_WEBHOOK_ALLOWED_HOSTS` 可限制域名。

## 6. 错误码

| 状态码 | 含义 |
|---|---|
| 202 | 新批次已接收 |
| 200 | 幂等重放，已存在且内容一致 |
| 400 | 参数错误：未知设备、群名不在白名单、时间窗已过期等 |
| 401 | API Key 错误 |
| 409 | `external_batch_id` 已存在但内容不同 |
| 503 | 服务端未配置 `AUTOGLM_INGEST_TOKEN` |

## 7. 行为说明

- **确定性发送**：服务不依赖通用 Agent 自由操作，而是按固定流程执行：
  重启微信 → 回到会话列表 → 精确搜索群名 → 打开群 → 视觉校验群标题 →
  清空草稿 → 选中「所有人」→ 注入正文 → 点击发送 → 校验输入框已清空。
  任一校验失败即标记 `failed`，**不会盲发**。
- **离散发送**：同一设备的 N 条消息在窗口内随机铺开，间隔不小于 `min_gap_seconds`；
  不同设备互相独立。
- **离线设备**：窗口内持续重试（默认每 60s），窗口结束仍离线则标记 `failed`。
- **失败不自动重发**：任务一旦开始执行，失败只记录状态并通过 Webhook 通知，
  避免重复发群消息；由操作员在 GUI 中「重新触发」。
- **内容保真**：正文通过 ADB Keyboard base64 注入，换行、标点和空格原样保留，
  不经过模型改写。
- **提及范围**：目前只把 `@所有人` 处理成真实提及；正文中的其他 `@某人`
  是普通文本，不会触发提醒。如果需要真实提醒某人，需要扩展提及选择流程。
- **视觉校验依赖模型**：服务必须配置可用的视觉模型，否则该条消息失败而不是盲发。
- **坐标依赖**：按 1080×2400、微信 8.0.7x 校准，换机型或大版本需要校准
  `AutoGLM_GUI/push/wechat_sender.py` 的 `WeChatProfile`。
- **人工干预**：GUI「推送批次」页面可查看每条消息的状态、错误、关联任务，并重新触发失败项。

## 8. 联调辅助

`deploy/examples/make_push_payload.py` 可生成一份带当前时间窗的验证 payload：

```bash
python deploy/examples/make_push_payload.py --window-minutes 5
curl -s -H "X-API-Key: $AUTOGLM_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @verify-payload.json \
  http://127.0.0.1:8000/api/ingest/push-batches/dry-run
```