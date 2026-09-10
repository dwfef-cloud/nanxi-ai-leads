# 获客系统 v1.0 · API 接口契约

> **所有子智能体必须严格遵守本契约。** 前端 `mock-data.js` 已按此契约写好，后端实现后前端零改动即可切换真实数据。
>
> 最后更新：2026-09-10

---

## 通用约定

| 项目 | 约定 |
|------|------|
| 基址 | `http://127.0.0.1:8000/api` |
| 鉴权 | 当前无（本地工具），后续加 Bearer Token |
| 请求体 | `application/json` |
| 响应格式 | 统一 JSON，错误时 `{ "detail": "错误信息" }` |
| 时间格式 | ISO 8601（`2026-08-25T10:30:00+00:00`），前端自行格式化 |
| ID 格式 | 32 位 hex 字符串（uuid4.hex） |
| 分页 | 当前列表接口不分页，全量返回；后续加 `page`/`page_size` |

---

## 枚举定义

### 线索来源 `LeadSource`
| 值 | 标签 | 说明 |
|----|------|------|
| `own_comment` | 自有视频评论区 | 在自己视频下评论的用户 |
| `competitor` | 对标账号监控 | 在竞品视频下评论的用户 |
| `inbound_dm` | 用户主动私信 | 用户主动私信主页（意向最高） |
| `fan_dm` | 粉丝私信 | 粉丝发的私信 |
| `referral` | 老客转介绍 | 成交客户推荐 |
| `lead_card` | 留资卡 | 通过留资表单进入 |

### 线索状态 `LeadStatus`（9 种状态机）
| 值 | 标签 | 说明 |
|----|------|------|
| `collected` | 已采集 | 刚入库，未处理 |
| `pending_outreach` | 待发送 | 已分配账号和话术，等待发送 |
| `throttled` | 频控暂缓 | 账号 R1 降速中，排队等待 |
| `sent` | 已私信 | 私信已发出，等待回复 |
| `replied` | 已回复 | 用户回复了私信，待人工跟进 |
| `wechat_added` | 已加微 ★ | 已添加企业微信（北极星指标） |
| `deal_won` | 已成交 | 签约成交 |
| `send_failed` | 发送失败 | 发送失败，可重试 |
| `rejected` | 已过滤 | 无意向/退订/黑名单，不再触达 |

### 商机阶段 `CustomerStage`（7 阶段）
| 值 | 标签 | 顺序 |
|----|------|------|
| `added` | 已加微 | 1 |
| `measured` | 已量房 | 2 |
| `proposal` | 方案中 | 3 |
| `quoted` | 已报价 | 4 |
| `negotiating` | 谈判中 | 5 |
| `won` | 已成交 | 6 |
| `lost` | 已流失 | 7 |

### 账号限流状态 `AccountLimitStatus`
| 值 | 说明 | 健康分区间 |
|----|------|-----------|
| `healthy` | 健康 | ≥60 |
| `warn` | 预警 | 40-59 |
| `throttled40` | R1 降速至 40/天 | 日频达 80 触发 |
| `safe_mode` | 安全模式（全量暂停） | <40 或 R3 触发 |
| `banned` | 已封禁 | 平台限流/封号 |

### 话术分类 `ScriptCategory`
| 值 | 说明 |
|----|------|
| `comment` | 评论区回复话术（评论引流策略核心） |
| `private_message` | 私信话术 |
| `wechat_guide` | 微信引导话术 |
| `objection` | 异议处理话术 |
| `nurture` | 加微后培育 SOP |

### 评论回复任务状态 `CommentReplyStatus`
| 值 | 说明 |
|----|------|
| `pending` | 待回复 |
| `replied` | 已回复，等待对方反应 |
| `user_replied` | 对方追评/回复了评论 |
| `user_dm` | 对方主动私信（转化成功 ★） |
| `ignored` | 对方无反应（超过 48h） |

---

## 一、工作台

### `GET /api/workbench`
今日运营总览（北极星导向）。

**响应：**
```json
{
  "date": "2026-08-25 · 周二",
  "today": {
    "newLeads": 12,
    "pendingSend": 5,
    "sentToday": 46,
    "wechatToday": 6,
    "followupsPending": 7,
    "followupsOverdue": 2,
    "deal": { "count": 1, "amount": 128000 }
  },
  "alerts": [
    { "level": "danger|warn|info", "text": "...", "action": "查看账号", "href": "#/health" }
  ],
  "activity": [
    { "time": "08-25 09:12", "type": "safe_mode", "level": "danger", "text": "..." }
  ]
}
```
**前端函数：** `getWorkbench()`

---

## 二、获客域

### 2.1 线索池

#### `GET /api/leads`
线索列表，支持筛选。

**查询参数：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | 按 LeadStatus 筛选，`all` 或不传=全部 |
| `source` | string | 按 LeadSource 筛选 |
| `keyword` | string | 昵称/评论/标签模糊搜索 |
| `min_score` | int | 最低意向分 |

**响应：** `Lead[]`（字段见数据模型）

#### `GET /api/leads/status-meta`
线索状态元数据（前端筛选器用）。

**响应：**
```json
{
  "collected": { "label": "已采集", "cls": "collected" },
  "pending_outreach": { "label": "待发送", "cls": "pending" },
  "throttled": { "label": "频控暂缓", "cls": "throttled" },
  "sent": { "label": "已私信", "cls": "sent" },
  "replied": { "label": "已回复", "cls": "replied" },
  "wechat_added": { "label": "已加微 ★", "cls": "wechat" },
  "deal_won": { "label": "已成交", "cls": "deal" },
  "send_failed": { "label": "发送失败", "cls": "failed" },
  "rejected": { "label": "已过滤", "cls": "rejected" }
}
```

#### `GET /api/leads/source-meta`
线索来源元数据。

**响应：**
```json
{
  "own_comment": { "label": "自有视频评论区", "cls": "sent" },
  "competitor": { "label": "对标账号监控", "cls": "throttled" },
  "inbound_dm": { "label": "用户主动私信", "cls": "replied" },
  "fan_dm": { "label": "粉丝私信", "cls": "pending" },
  "referral": { "label": "老客转介绍", "cls": "wechat" },
  "lead_card": { "label": "留资卡", "cls": "mid" }
}
```

#### `GET /api/leads/{lead_id}`
线索详情。**响应：** `Lead`

#### `POST /api/leads`
手动新建线索。**请求体：** `LeadCreate`（nickname/source/comment/video/platform 等）

#### `PATCH /api/leads/{lead_id}`
更新线索（状态/标签/备注/评分等）。

#### `POST /api/leads/{lead_id}/wechat_added`
手动标记加微（IMP-002 降级入口）。

**请求体：** `{ "manual": true }`
**响应：** `{ "ok": true, "lead_id": "...", "manual": true }`

#### `POST /api/leads/import`
批量导入线索（采集任务完成后调用）。

---

### 2.2 私信任务队列

#### `GET /api/dm/queue`
私信发送队列（执行器视角）。

**响应：**
```json
[
  {
    "leadId": "LD-1023",
    "nickname": "王女士家半包",
    "hue": 28,
    "account": "装修案例·阿明",
    "variant": "A",
    "status": "pending_outreach",
    "detail": "计划今天 10:40 发送（随机间隔 3-8 分钟）"
  }
]
```
**状态值：** `pending_outreach` / `throttled` / `send_failed` / `sent` / `replied`

---

### 2.3 话术库

#### `GET /api/scripts`
话术列表（含变体统计）。

**查询参数：** `category`（comment/private_message/wechat_guide/objection/nurture）

**响应：**
```json
[
  {
    "id": "SC-001",
    "name": "装修 · 主话术",
    "industry": "装修",
    "category": "private_message",
    "isMain": true,
    "active": true,
    "intro": "...",
    "welcomeMsg": "...",
    "variants": [
      {
        "id": "A",
        "text": "...",
        "weight": 70,
        "status": "active",
        "sent": 214,
        "replied": 79,
        "wechatAdded": 24,
        "convRate": 11.2,
        "sampleEnough": true
      }
    ]
  }
]
```

#### `GET /api/scripts/templates`
行业话术模板包。

**响应：**
```json
[
  { "industry": "装修", "count": 8, "desc": "首次触达×3 / 报价跟进×2 / 加微引导×3", "installed": true },
  { "industry": "教育", "count": 0, "desc": "v2 规划中", "installed": false }
]
```

#### `POST /api/scripts`
新建话术。#### `POST /api/scripts/{id}/variants`
新增话术变体。

#### `PATCH /api/scripts/{id}/variants/{variant_id}`
更新变体（权重/状态/文本）。R2 命中时自动切换状态为 `switched_off`。

---

### 2.4 抖音账号

#### `GET /api/accounts`
账号列表（含健康分和限流状态）。

**响应：**
```json
[
  {
    "id": "acc-01",
    "nickname": "装修案例·阿明",
    "platform": "douyin",
    "healthScore": 86,
    "limitStatus": "healthy",
    "dailyOutreach": 46,
    "dailyLimit": 80,
    "factors": {
      "dailyFreq": "good",
      "banHistory": "good",
      "login": "good",
      "unsubscribe": "good",
      "complaint": "good"
    },
    "lastBanReason": null,
    "updatedAt": "2026-08-25 09:12"
  }
]
```

#### `POST /api/accounts`
新增账号。

#### `PATCH /api/accounts/{id}`
更新账号（状态/备注/健康分因子）。

---

### 2.5 评论回复任务（评论引流策略核心 · 新增）

#### `GET /api/comments/tasks`
评论回复任务列表。

**查询参数：** `status`（pending/replied/user_replied/user_dm/ignored）

**响应：**
```json
[
  {
    "id": "...",
    "leadId": "LD-1024",
    "commentContent": "家里90平老房子想翻新，求靠谱的装修公司",
    "videoTitle": "老房改造避坑指南",
    "replyContent": "我们老城区旧改做了40+户，加我发您同小区改造对比",
    "status": "replied",
    "account": "装修案例·阿明",
    "repliedAt": "2026-08-25T09:30:00+00:00",
    "userVisited": false,
    "userDm": false,
    "userRepliedComment": false
  }
]
```

#### `POST /api/comments/tasks`
创建评论回复任务（从高意向线索生成）。

#### `PATCH /api/comments/tasks/{id}`
更新任务状态（标记已回复/对方私信/对方追评）。

### 2.6 私信收件箱（评论引流转化闭环）

承接用户主动私信，支持会话列表、详情查看、回复、标记加微转客户。

#### `GET /api/dm/inbox`
收件箱会话列表，按最后消息时间倒序。

**查询参数：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | 按状态筛选：new / replied / wechat_added / closed / all（默认 all） |

**响应：** `InboxConversationItem[]`
```json
[
  {
    "id": "conv-001",
    "lead_id": "lead-001",
    "customer_id": null,
    "nickname": "装修业主张三",
    "avatar_hue": 172,
    "last_message": "请问多少钱一平米？",
    "last_message_at": "2026-08-25T10:30:00+00:00",
    "unread_count": 2,
    "status": "new"
  }
]
```

**状态枚举：** `new`（新会话未回复）/ `replied`（已回复）/ `wechat_added`（已加微）/ `closed`（已关闭）

#### `GET /api/dm/inbox/{conversation_id}`
会话详情 + 全部消息（按时间正序），同时标记所有入站消息为已读。

**响应：** `InboxConversationDetail`
```json
{
  "id": "conv-001",
  "lead_id": "lead-001",
  "customer_id": null,
  "nickname": "装修业主张三",
  "avatar_hue": 172,
  "status": "new",
  "messages": [
    {
      "id": "msg-001",
      "direction": "in",
      "sender": "user",
      "content": "请问多少钱一平米？",
      "is_read": true,
      "script_id": null,
      "created_at": "2026-08-25T10:30:00+00:00"
    }
  ]
}
```
**消息方向：** `in`=用户发 / `out`=我们发

#### `POST /api/dm/inbox/{conversation_id}/reply`
发送回复（创建一条 outbound 消息，更新会话最后消息时间）。

**请求体：**
```json
{
  "content": "您好，我们根据面积和风格报价，方便说下您的户型吗？",
  "script_id": null
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 回复内容 |
| `script_id` | string | 否 | 使用的话术 ID（可选） |

**响应：** `InboxMessageItem`（新建的消息对象）

#### `POST /api/dm/inbox/{conversation_id}/mark-wechat`
标记加微转客户：关闭会话、线索标记加微、自动创建客户。

**请求体：**
```json
{
  "wechat_id": "zhangsan_wx",
  "customer_name": "张三",
  "phone": "13800138000"
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `wechat_id` | string | 是 | 客户微信号 |
| `customer_name` | string | 否 | 客户姓名（留空则用昵称） |
| `phone` | string | 否 | 手机号 |

**响应：**
```json
{
  "ok": true,
  "conversation_id": "conv-001",
  "customer_id": "cust-001",
  "lead_id": "lead-001"
}
```

#### `POST /api/dm/inbox/simulate`
模拟接收私信（开发测试用）：创建/获取会话 + 创建入站消息。

**请求体：**
```json
{
  "nickname": "测试用户",
  "content": "你好，咨询一下",
  "lead_id": null
}
```
**响应：** `{ "ok": true, "conversation_id": "...", "message_id": "...", "lead_id": "..." }`

### 2.7 采集模块（MediaCrawler 对接）

采集任务管理与结果入库。与 `/api/crawler/*`（旧版采集入口，保留兼容）并存，新代码请使用 `/api/crawl/*`。

#### `GET /api/crawl/tasks`
采集任务列表。

**查询参数：** `status`（pending / running / completed / failed）

**响应：** `CrawlTaskRead[]`
```json
[
  {
    "id": "crawl-001",
    "name": "装修关键词采集",
    "crawl_type": "comment",
    "keyword": "装修 报价",
    "competitor_account": "",
    "video_url": "",
    "source": "own_comment",
    "intent_keywords": "多少钱,报价,求推荐",
    "excluded_keywords": "广告,招聘",
    "max_comments": 100,
    "status": "running",
    "collected_count": 45,
    "imported_count": 40,
    "error_message": "",
    "started_at": "2026-08-25T09:00:00+00:00",
    "finished_at": null,
    "created_at": "2026-08-25T08:55:00+00:00",
    "updated_at": "2026-08-25T09:10:00+00:00"
  }
]
```

**采集类型 `crawl_type`：** `comment`（评论区采集）/ `search`（搜索采集）/ `profile`（主页采集）

**任务状态 `status`：** `pending`（待启动）/ `running`（采集中）/ `completed`（已完成）/ `failed`（失败）

#### `POST /api/crawl/tasks`
创建采集任务（201 Created）。至少需要提供一个采集入口：keyword / competitor_account / video_url。

**请求体：** `CrawlTaskCreate`
```json
{
  "name": "老房翻新关键词采集",
  "crawl_type": "comment",
  "keyword": "老房翻新",
  "competitor_account": "",
  "video_url": "",
  "source": "own_comment",
  "intent_keywords": "多少钱,报价,求推荐",
  "excluded_keywords": "广告,招聘",
  "max_comments": 100
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 否 | 任务名称 |
| `crawl_type` | string | 否 | comment / search / profile（默认 comment） |
| `keyword` | string | 否* | 搜索关键词（*三选一必填） |
| `competitor_account` | string | 否* | 对标账号（*三选一必填） |
| `video_url` | string | 否* | 指定视频URL（*三选一必填） |
| `source` | string | 否 | own_comment / competitor（默认 own_comment） |
| `intent_keywords` | string | 否 | 高意向关键词（逗号分隔） |
| `excluded_keywords` | string | 否 | 排除关键词（逗号分隔） |
| `max_comments` | int | 否 | 最大采集评论数（默认100，上限10000） |

**错误：** 400 — 三个采集入口全为空时返回 `{"detail":"至少需要提供一个采集入口：keyword / competitor_account / video_url"}`

#### `POST /api/crawl/tasks/{task_id}/start`
启动采集任务。

**响应：** `CrawlTaskRead`（status → running）
**错误：** 404 — 任务不存在

#### `POST /api/crawl/tasks/{task_id}/stop`
停止采集任务。

**响应：** `CrawlTaskRead`
**错误：** 404 — 任务不存在

#### `GET /api/crawl/tasks/{task_id}/status`
采集任务状态（含进度）。

**响应：** `CrawlTaskStatus`
```json
{
  "id": "crawl-001",
  "status": "running",
  "collected_count": 45,
  "imported_count": 40,
  "max_comments": 100,
  "error_message": "",
  "started_at": "2026-08-25T09:00:00+00:00",
  "finished_at": null
}
```

#### `DELETE /api/crawl/tasks/{task_id}`
删除采集任务（204 No Content）。

**错误：** 404 — 任务不存在

#### `POST /api/crawl/import`
采集结果批量入库为线索。MediaCrawler 输出的评论数据通过此端点自动入库，自动评分（高意向关键词加权）。

**请求体：** `CrawlImportRequest`
```json
{
  "task_id": "crawl-001",
  "source": "own_comment",
  "keyword": "老房翻新",
  "items": [
    {
      "nickname": "装修业主张三",
      "content": "请问多少钱一平米？想了解详细报价",
      "comment_id": "cid-001",
      "aweme_id": "aweme-001",
      "video_title": "老房改造避坑指南",
      "source_url": "https://www.douyin.com/video/aweme-001",
      "user_id": "uid-001",
      "create_time": "2026-08-25 10:30:00"
    }
  ]
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 否 | 关联采集任务 ID |
| `source` | string | 否 | own_comment / competitor（默认 own_comment） |
| `keyword` | string | 否 | 采集关键词 |
| `items` | array | 是 | 评论数据列表（不能为空） |
| `items[].nickname` | string | 否 | 评论用户昵称 |
| `items[].content` | string | 否 | 评论内容 |
| `items[].comment_id` | string | 否 | 评论唯一ID（去重用） |
| `items[].aweme_id` | string | 否 | 视频ID |
| `items[].video_title` | string | 否 | 视频标题 |
| `items[].source_url` | string | 否 | 来源URL |
| `items[].user_id` | string | 否 | 用户ID |
| `items[].create_time` | string | 否 | 评论时间 |

**响应：** `CrawlImportResponse`
```json
{
  "imported": 1,
  "skipped": 0,
  "task_id": "crawl-001",
  "leads": ["lead-001"]
}
```
**错误：** 400 — items 为空时返回 `{"detail":"items 不能为空"}`

---

## 三、AI 大模型模块

支持 DashScope（通义千问）和豆包（Doubao/Volcengine Ark）双 Provider，通过环境变量 `AI_PROVIDER=dashscope|doubao` 切换。API Key 从 `DASHSCOPE_API_KEY` / `DOUBAO_API_KEY` 读取，也可通过 `PUT /api/ai/settings` 运行时配置。

**通用错误：**
- `503` — 未配置 API Key：`{"detail":"请配置 API Key（环境变量 DASHSCOPE_API_KEY 或在设置页填写），当前 AI 服务不可用"}`
- `502` — 上游 AI 服务错误（鉴权失败/超时/网络异常），含上游错误详情
- 所有 AI 接口超时 10 秒（业务画像分析 45 秒），AI 服务不可用时不影响其他端点

### 3.1 AI 设置

#### `GET /api/ai/settings`
获取当前 AI 配置。

**响应：**
```json
{
  "provider": "dashscope",
  "configured": true,
  "masked_key": "sk-a****1234",
  "model": "qwen-plus"
}
```
| 字段 | 类型 | 说明 |
|------|------|------|
| `provider` | string | dashscope / doubao |
| `configured` | bool | 是否已配置 API Key |
| `masked_key` | string | 脱敏后的 Key（前4+****+后4），未配置时为空 |
| `model` | string | 当前使用的模型名 |

#### `PUT /api/ai/settings`
更新 AI 配置（运行时生效，无需重启）。

**请求体：**
```json
{
  "provider": "dashscope",
  "api_key": "sk-你的key",
  "model": "qwen-plus"
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `provider` | string | 否 | dashscope / doubao（默认 dashscope） |
| `api_key` | string | 否 | API Key（传空字符串不清除已有 Key） |
| `model` | string | 否 | 模型名（默认 qwen-plus；豆包用 endpoint_id） |

**响应：** 同 `GET /api/ai/settings`

### 3.2 线索画像分析

#### `POST /api/ai/analyze`
分析单条线索的意向等级、需求标签、推荐话术和跟进建议。成功后自动回写 Lead 的 `intent_level` / `tags` / `customer_need`。

**请求体：**
```json
{
  "nickname": "装修业主张三",
  "comment": "请问多少钱一平米？想了解详细报价",
  "video_title": "老房改造避坑指南",
  "source_keyword": "装修 报价"
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `nickname` | string | 否 | 用户昵称（用于匹配线索回写） |
| `comment` | string | 否 | 评论原文 |
| `video_title` | string | 否 | 来源视频标题 |
| `source_keyword` | string | 否 | 来源关键词 |

**响应：**
```json
{
  "intent_level": "A",
  "customer_need": "明确询价，关注装修报价和方案",
  "tags": ["求报价", "高意向", "老房翻新"],
  "recommended_script_category": "private_message",
  "follow_up_suggestion": "立即私信发送报价区间，引导加微信发详细方案",
  "raw_reasoning": "评论中明确询问价格，属于高意向询价行为"
}
```
| 字段 | 类型 | 说明 |
|------|------|------|
| `intent_level` | string | A=高意向 / B=中意向 / C=低意向 |
| `customer_need` | string | 客户核心需求一句话总结 |
| `tags` | string[] | 需求标签（3-6个） |
| `recommended_script_category` | string | comment / private_message / wechat_guide / objection / nurture |
| `follow_up_suggestion` | string | 可执行的跟进建议 |
| `raw_reasoning` | string | 判断依据简述 |

### 3.3 智能草稿

#### `POST /api/ai/draft`
根据话术类别 + 用户画像 + 评论内容，生成个性化回复草稿。不自动发送，返回草稿供人工确认。

**请求体：**
```json
{
  "lead_id": "lead-001",
  "script_category": "private_message",
  "user_comment": "请问多少钱一平米？",
  "style": "casual"
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `lead_id` | string | 是 | 线索ID（用于拉取画像） |
| `script_category` | string | 否 | comment / private_message / wechat_guide / objection / nurture（默认 private_message） |
| `user_comment` | string | 否 | 用户最新评论/消息内容（可选） |
| `style` | string | 否 | formal=更正式 / casual=更口语 / short=更简短（默认 casual） |

**响应：**
```json
{
  "draft": "您好呀！看到您问价格，我们这边是根据面积和风格来定的，方便说下您家大概多少平、想装什么风格吗？我给您个参考报价～",
  "style": "casual",
  "lead_nickname": "装修业主张三"
}
```

### 3.4 评论回复建议

#### `POST /api/ai/comment-suggestion`
根据用户评论生成 3 条评论回复建议（钩子型/价值型/提问型），用于评论回复任务页。

**请求体：**
```json
{
  "comment": "家里90平老房子想翻新，求靠谱的装修公司",
  "video_title": "老房改造避坑指南",
  "source_keyword": "装修"
}
```
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `comment` | string | 是 | 用户评论原文 |
| `video_title` | string | 否 | 所在视频标题 |
| `source_keyword` | string | 否 | 来源关键词 |

**响应：**
```json
{
  "suggestions": [
    {
      "type": "hook",
      "label": "钩子型",
      "content": "90平老房翻新我们做了很多同户型，私信发您改造前后对比图～"
    },
    {
      "type": "value",
      "label": "价值型",
      "content": "老房翻新重点在水电改造和空间利用，建议先确定预算区间再选方案"
    },
    {
      "type": "question",
      "label": "提问型",
      "content": "您家老房是几室几厅呢？想翻新哪些区域呀？"
    }
  ]
}
```
**建议类型：** `hook`=钩子型（留悬念引导私信）/ `value`=价值型（给专业信息建立信任）/ `question`=提问型（开放式问题引导表达）

### 3.5 兼容接口（保留）

| 端点 | 说明 |
|------|------|
| `POST /api/ai/profile-analysis` | 业务画像分析（原有接口，基于产品/行业生成获客关键词规则） |
| `POST /api/ai/draft-legacy` | 旧版硬编码草稿（原有接口，新代码请用 `POST /api/ai/draft`） |

---

## 四、客户域

### 4.1 客户与商机

#### `GET /api/customers`
客户列表（含商机阶段）。

**查询参数：** `stage`（按 CustomerStage 筛选）

**响应：**
```json
[
  {
    "id": "CU-001",
    "leadId": "LD-1016",
    "name": "叮当一家",
    "source": "own_comment",
    "stage": "won",
    "estValue": 128000,
    "dealAmount": 128000,
    "dealAt": "2026-08-25T10:30:00+00:00",
    "wechatAddedAt": "2026-08-24T09:12:00+00:00",
    "manual": false,
    "referrer": null,
    "nextAction": null,
    "nextAt": null,
    "hue": 260,
    "logs": [
      { "time": "08-23 21:05", "text": "私信引导加微（变体A）", "by": "系统" }
    ]
  }
]
```

#### `GET /api/customers/stage-meta`
商机阶段元数据。

**响应：**
```json
{
  "added": { "label": "已加微", "cls": "wechat", "order": 1 },
  "measured": { "label": "已量房", "cls": "sent", "order": 2 },
  "proposal": { "label": "方案中", "cls": "pending", "order": 3 },
  "quoted": { "label": "已报价", "cls": "throttled", "order": 4 },
  "negotiating": { "label": "谈判中", "cls": "mid", "order": 5 },
  "won": { "label": "已成交", "cls": "deal", "order": 6 },
  "lost": { "label": "已流失", "cls": "rejected", "order": 7 }
}
```

#### `POST /api/customers`
手动新建客户（线索加微后自动创建，或手动录入）。

#### `POST /api/customers/{id}/stage`
推进商机阶段（一键点选，不填表单）。

**请求体：** `{ "stage": "quoted" }`
**响应：** `{ "ok": true }`

#### `POST /api/customers/{id}/deal`
成交录入（≤3 次点击）。

**请求体：** `{ "amount": 128000 }`
**响应：** `{ "ok": true, "customerId": "...", "amount": 128000 }`

#### `POST /api/customers/{id}/lost`
标记流失。

**请求体：** `{ "reason": "预算不匹配，选了施工队" }`
**响应：** `{ "ok": true }`

#### `GET /api/customers/{id}/logs`
客户跟进日志（timeline）。

---

### 4.2 今日跟进

#### `GET /api/followups`
跟进待办列表。

**查询参数：** `date`（`today` / `overdue` / `upcoming`）

**响应：**
```json
[
  {
    "id": "FU-01",
    "customerId": "CU-007",
    "customerName": "翡翠湾陈先生",
    "type": "报价跟进",
    "text": "发送报价二版（按 96㎡ 调整主材）",
    "due": "今天 15:00",
    "overdue": false,
    "done": false
  }
]
```

#### `POST /api/followups/{id}/complete`
标记完成。**响应：** `{ "ok": true }`

#### `POST /api/followups`
新建跟进待办。

---

## 五、分析域

### `GET /api/dashboard/funnel`
六级转化漏斗 + 成本 + ROI + 14 天加微趋势。

**响应：**
```json
{
  "window": "近 30 天",
  "stages": [
    { "key": "exposure", "label": "评论区曝光", "value": 12840 },
    { "key": "collected", "label": "线索入库", "value": 342 },
    { "key": "outreach", "label": "私信触达", "value": 285 },
    { "key": "replied", "label": "私信回复", "value": 96 },
    { "key": "wechat", "label": "加企微 ★", "value": 41 },
    { "key": "deal", "label": "成交", "value": 9 }
  ],
  "cost": {
    "totalSpend": 2680,
    "perLead": 7.84,
    "perWechatAdd": 65.4,
    "perDeal": 297.8,
    "avgDealAmount": 18600
  },
  "wechatTrend": [
    { "date": "08-12", "v": 1 }
  ]
}
```

### `GET /api/dashboard/attribution`
6 触点归因 + 内容榜 + 话术贡献。

**响应：**
```json
{
  "window": "近 30 天",
  "bySource": [
    { "source": "own_comment", "label": "自有视频评论区", "leads": 186, "wechat": 20, "deal": 4, "pilot": false }
  ],
  "topVideos": [
    { "video": "8万块爆改79㎡客厅", "leads": 38, "wechat": 7, "dealAmount": 80000 }
  ]
}
```

### `GET /api/dashboard/compliance`
合规风险总览 + R1/R2/R3 规则 + 审计日志。

**响应：**
```json
{
  "summary": {
    "unsubscribe7d": 7,
    "blacklistTotal": 23,
    "blacklistDelta7d": 2,
    "complaints7d": 1,
    "auditEvents": 156
  },
  "rules": [
    { "id": "R1", "desc": "单账号日频 ≥ 80 → 降速至 40", "status": "hit", "hitAt": "08-25 08:40", "target": "全屋定制·凯文" }
  ],
  "audit": [
    { "time": "08-25 09:12", "type": "safe_mode", "level": "danger", "text": "..." }
  ]
}
```

---

## 六、运行域

### `GET /api/runtime`
全局运行态。

**响应：**
```json
{
  "safeMode": {
    "active": false,
    "reason": "",
    "triggeredAt": null,
    "triggerSource": null
  },
  "taskRunning": true
}
```

### `POST /api/compliance/safe-mode/enter`
进入安全模式（全量暂停）。

**请求体：** `{ "source": "manual|r3|health_score", "reason": "..." }`

### `POST /api/compliance/safe-mode/exit`
退出安全模式（必须人工确认）。

### `POST /api/compliance/pause`
一键全量暂停。

### `POST /api/compliance/resume`
恢复运行。

---

## 七、配置域 / 上手向导

### `POST /api/onboarding/wizard`
5 步上手向导提交（绑账号→选行业话术→设频控→启动→看结果）。

**请求体：** 向导各步配置的聚合对象。

### 工作台配置（已有，保留）
- `GET/PUT /api/workbench/product` — 产品知识库
- `GET/PUT /api/workbench/audience` — 目标客户
- `GET/PUT /api/workbench/scripts` — 话术策略
- `GET/PUT /api/workbench/wechat` — 微信转化设置

---

## 八、数据模型字段速查

### Lead（线索）
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 32位hex |
| `nickname` | string | 抖音昵称 |
| `source` | LeadSource | 6种触点来源 |
| `comment` | string | 评论原文 |
| `video` | string | 来源视频标题 |
| `status` | LeadStatus | 9种状态 |
| `score` | int | 意向分 0-100 |
| `intent` | high/mid/low | 前端展示标签 |
| `intent_level` | A/B/C/D | 评分等级 |
| `tags` | string[] | 标签 |
| `account` | string\|null | 负责账号 |
| `wechat_added_at` | datetime\|null | 加微时间 |
| `manual` | bool | 手动标记加微 |
| `deal_amount` | float\|null | 成交金额 |
| `deal_at` | datetime\|null | 成交时间 |
| `referral_note` | string\|null | 转介绍备注 |
| `reject_reason` | string\|null | 过滤原因 |
| `fail_note` | string\|null | 失败说明 |
| `throttled_note` | string\|null | 频控说明 |
| `hue` | int | 头像色相 |
| `created_at` / `updated_at` | datetime | 时间戳 |

### Customer（客户）
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | |
| `lead_id` | string\|null | 关联线索（手动录入可为空） |
| `name` | string | 客户名 |
| `source` | LeadSource | 来源 |
| `stage` | CustomerStage | 7阶段 |
| `est_value` | float | 预估金额 |
| `deal_amount` | float\|null | 成交金额 |
| `deal_at` | datetime\|null | 成交时间 |
| `wechat_added_at` | datetime\|null | 加微时间 |
| `referrer` | string\|null | 转介绍推荐人 |
| `next_action` | string\|null | 下一步动作 |
| `next_at` | string\|null | 下一步时间（展示用） |
| `lost_reason` | string\|null | 流失原因 |
| `lost_at` | datetime\|null | 流失时间 |

### Account（账号）
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` / `nickname` / `platform` | | |
| `health_score` | int | 0-100 |
| `limit_status` | AccountLimitStatus | |
| `daily_outreach` / `daily_limit` | int | |
| `factors` | dict | 五维因子（dailyFreq/banHistory/login/unsubscribe/complaint） |
| `last_ban_reason` / `r1_note` / `r3_note` | string\|null | |

---

## 九、前端 Mock → 真实 API 替换对照表

| 前端函数 | 真实 API |
|----------|----------|
| `getWorkbench()` | `GET /api/workbench` |
| `getLeads(params)` | `GET /api/leads?status=...&source=...` |
| `getLeadStatusMeta()` | `GET /api/leads/status-meta` |
| `getSourceMeta()` | `GET /api/leads/source-meta` |
| `getDmQueue()` | `GET /api/dm/queue` |
| `markWechatAdded(id)` | `POST /api/leads/{id}/wechat_added` |
| `getScripts()` | `GET /api/scripts` |
| `getScriptTemplates()` | `GET /api/scripts/templates` |
| `getAccounts()` | `GET /api/accounts` |
| `getCustomers()` | `GET /api/customers` |
| `getStageMeta()` | `GET /api/customers/stage-meta` |
| `getFollowups()` | `GET /api/followups?date=today` |
| `completeFollowup(id)` | `POST /api/followups/{id}/complete` |
| `advanceStage(id, stage)` | `POST /api/customers/{id}/stage` |
| `recordDeal(id, amount)` | `POST /api/customers/{id}/deal` |
| `markLost(id, reason)` | `POST /api/customers/{id}/lost` |
| `getFunnel()` | `GET /api/dashboard/funnel` |
| `getAttribution()` | `GET /api/dashboard/attribution` |
| `getCompliance()` | `GET /api/dashboard/compliance` |
| `getRuntime()` | `GET /api/runtime` |
| `enterSafeMode()` | `POST /api/compliance/safe-mode/enter` |
| `exitSafeMode()` | `POST /api/compliance/safe-mode/exit` |
| `pauseAll()` / `resumeAll()` | `POST /api/compliance/pause` \| `/resume` |
| `submitWizard(payload)` | `POST /api/onboarding/wizard` |
| `getAISettings()` | `GET /api/ai/settings` |
| `aiAnalyzeLead(payload)` | `POST /api/ai/analyze` |
| `aiSmartDraft(payload)` | `POST /api/ai/draft` |
| `aiCommentSuggestion(payload)` | `POST /api/ai/comment-suggestion` |
| `getDmInbox(status?)` | `GET /api/dm/inbox?status=...` |
| `getDmConversation(id)` | `GET /api/dm/inbox/{id}` |
| `sendDmReply(id, content, scriptId?)` | `POST /api/dm/inbox/{id}/reply` |
| `markDmWechat(id, payload)` | `POST /api/dm/inbox/{id}/mark-wechat` |
| `getCrawlTasks(status?)` | `GET /api/crawl/tasks?status=...` |
| `createCrawlTask(payload)` | `POST /api/crawl/tasks` |
| `startCrawlTask(id)` | `POST /api/crawl/tasks/{id}/start` |
| `stopCrawlTask(id)` | `POST /api/crawl/tasks/{id}/stop` |
| `getCrawlTaskStatus(id)` | `GET /api/crawl/tasks/{id}/status` |
| `deleteCrawlTask(id)` | `DELETE /api/crawl/tasks/{id}` |
| `importCrawlResults(payload)` | `POST /api/crawl/import` |

> **P2 新增端点（已对接）：**
> - AI 大模型：`GET/PUT /api/ai/settings`、`POST /api/ai/analyze`、`POST /api/ai/draft`、`POST /api/ai/comment-suggestion`
> - 私信收件箱：`GET /api/dm/inbox`、`GET /api/dm/inbox/{id}`、`POST /api/dm/inbox/{id}/reply`、`POST /api/dm/inbox/{id}/mark-wechat`、`POST /api/dm/inbox/simulate`
> - 采集模块：`GET/POST /api/crawl/tasks`、`POST /api/crawl/tasks/{id}/start|stop`、`GET /api/crawl/tasks/{id}/status`、`DELETE /api/crawl/tasks/{id}`、`POST /api/crawl/import`
> - 评论回复任务：`GET/POST/PATCH /api/comments/tasks`，对应前端 `#/comments` 视图
