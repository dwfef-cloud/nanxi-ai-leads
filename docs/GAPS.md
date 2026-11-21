# 南溪AI获客系统 — 已知缺失与待完善清单

## 一、埋点缺失（已确认）✅ 全部修复

### ✅ 1. 线索状态变更不自动写事件
- **现状**：线索状态从 new→contacted→dm_sent→user_replied→wechat_added→converted 变更时，只更新 status 字段，不自动写 behavior_events
- **影响**：无法计算每个环节的停留时长、转化耗时
- **修复方向**：在 lead_service 的状态变更方法中，自动写入 behavior_events（event_type=status_change, content=from→to）
- **✅ 已修复**：`lead_service._log_event()` 在 `update_lead`/`batch_update_status`/`enqueue`/`mark_wechat_added` 中自动写 behavior_events；`customer_service.record_deal` 写 `deal_won`；`customer_service.mark_lost` 写 `customer_lost`

### ✅ 2. 话术效果闭环缺失
- **现状**：评论任务/私信回复记录了 script_id/variant_id，但没有自动关联最终转化结果
- **影响**：无法计算每条话术的"评论→私信→加微→成交"转化率，无法做话术 A/B 测试和自动优化
- **修复方向**：建立 script_usage 表，记录每次话术使用（script_id, variant_id, lead_id, channel, used_at），并在线索加微/成交时回写转化结果
- **✅ 已修复**：`script_usage` 表已建（id/script_id/variant_id/lead_id/channel/result/used_at/created_at）；`comment_task_service`/`dm_sender_service`/`dm_inbox_service` 在发送时自动 `record_script_usage()`；`lead_service.mark_wechat_added`/`dm_inbox_service.mark_wechat`/`customer_service.record_deal` 自动 `mark_script_result()` 回写 converted

### ✅ 3. 账号操作埋点不足
- **现状**：账号表有 daily_outreach/today_sent/today_success，但没有独立的操作事件记录
- **影响**：无法追踪每个账号的发送成功率趋势、被限流时间点、恢复时间
- **修复方向**：account_events 表，记录每次发送（account_id, action, success, error, timestamp）
- **✅ 已修复**：`account_events` 表已建（id/account_id/action/detail/success/created_at）；`comment_task_service` 记录 `comment_sent`；`dm_sender_service` 记录 `dm_sent`/`throttled`/`error`；action 枚举含 login/comment_sent/dm_sent/throttled/error/recovered

### ✅ 4. 流失原因无结构化分类
- **现状**：线索丢失只有 lost_reason 文本字段
- **影响**：无法统计流失原因分布（价格高/竞品/不需要/联系不上等）
- **修复方向**：lost_reason 改为枚举 + 备注，或增加 lost_category 字段
- **✅ 已修复**：leads/customers 表新增 `lost_reason_category`（枚举：price/competitor/no_need/timing/contact_lost/other）+ `lost_reason_note`（自由文本备注）；`LeadUpdate`/`LostInput` schema 加 Literal 校验；已有数据自动迁移为 category=other

### ✅ 5. behavior_events 自动写入不足
- **现状**：behavior_events 主要靠手动调用 API 记录，很多业务操作发生时不自动写
- **影响**：事件流不完整，无法还原用户完整旅程
- **修复方向**：在关键 service 方法中嵌入事件写入（评论发送、私信发送、加微、成交、流失等）
- **✅ 已修复**：
  - 评论：`comment_task_service` 发回复写 `comment_sent`，用户追评写 `comment_replied`
  - 私信发送：`dm_sender_service.send_one` 写 `dm_sent`/`dm_throttled`/`dm_failed`
  - 私信回复：`dm_inbox_service.send_reply` 写 `dm_sent`
  - 私信接收：`dm_inbox_service.simulate_inbound` 写 `dm_received`
  - 加微：`lead_service.mark_wechat_added` + `dm_inbox_service.mark_wechat` 写 `wechat_added`
  - 成交：`customer_service.record_deal` 写 `deal_won`
  - 流失：`customer_service.mark_lost` 写 `customer_lost`
  - 所有埋点均 try-except 包裹，失败不影响主流程

---

## 二、全面测试发现的缺失（2026-09-10 四域并行测试）

> 测试方式：4个子智能体并行，Python requests 实发请求 + 浏览器遍历20个前端页面，覆盖获客域/客户域/系统能力/前端流程。共测试端点 100+，发现问题 50+ 项。

### 统计概览

| 严重度 | 数量 | 说明 |
|--------|------|------|
| P0 阻断核心流程 | 4 | 必须立即修复 |
| P1 影响主要功能 | 20 | 本周内修复 |
| P2 体验问题 | 20 | 迭代修复 |
| P3 优化建议 | 15 |  backlog |

---

### P0 — 阻断核心流程（4项）✅ 全部修复

#### ✅ P0-1 客户页面完全空白：`/customers/stage-meta` 路由被动态路由吞掉
- **模块**：客户域 + 前端
- **问题**：`GET /api/customers/stage-meta` 返回 404 `{"detail":"Customer not found"}`。FastAPI 路由注册顺序错误：`GET /{customer_id}` 定义在 `GET /stage-meta` 之前，"stage-meta" 被当作 customer_id 匹配。
- **复现**：`curl.exe http://127.0.0.1:8000/api/customers/stage-meta` → 404；浏览器访问 `#/customers` → 主内容区空白（前端 `Promise.all` 因 stage-meta 失败整体 reject）
- **影响**：客户与商机页面完全不可用，阶段筛选器无法渲染
- **修复方向**：将 `/stage-meta` 路由移到 `/{customer_id}` 之前注册，或改为 `/customers/meta/stage` 非动态路径

#### ✅ P0-2 线索加微后不自动创建客户，转化链路断裂
- **模块**：获客域→客户域联动
- **问题**：`POST /api/leads/{id}/wechat_added` 返回 `{ok:true}` 但客户列表中无关联客户。线索→客户必须手动创建。
- **复现**：创建线索 → `POST /leads/{id}/wechat_added {"manual":true}` → `GET /customers` 无新客户
- **影响**：核心转化流程断裂，北极星指标（加微）无法自动流转到客户管理
- **修复方向**：在 `lead_service.mark_wechat_added()` 中自动调用 `customer_service.create()`，关联 lead_id，stage=added

#### ✅ P0-3 安全模式退出后状态未持久化，系统仍暂停
- **模块**：合规/运行域
- **问题**：`POST /api/compliance/safe-mode/exit` 返回成功，但 `GET /api/runtime` 仍显示 `safeMode.active=true`，DB runtime_state 未更新。
- **复现**：enter safe-mode → exit safe-mode → GET /runtime → active 仍为 true
- **影响**：退出安全模式后系统仍处于暂停状态，无法恢复发送
- **修复方向**：`safe-mode/exit` 接口需同步更新 DB runtime_state 的 active=0，而非仅改内存

#### ✅ P0-4 新采集线索无法发起私信：collected→pending_outreach 无流转入口
- **模块**：获客域状态机
- **问题**：采集导入的线索状态为 `collected`，但 `POST /api/dm/send/{leadId}` 只允许 `pending_outreach` 状态。前端无任何"移入发送队列"操作入口。
- **复现**：`POST /crawl/import` 导入线索 → `POST /dm/send/{id}` → 400 "线索状态为 collected，仅 pending_outreach 可发送"
- **影响**：采集→线索→私信的核心获客流程在此断裂
- **修复方向**：增加 `POST /api/leads/{id}/enqueue` 接口（collected→pending_outreach），前端线索池/私信页增加"移入发送队列"按钮；或采集导入高意向线索时自动设为 pending_outreach

---

### P1 — 影响主要功能（20项）

#### ✅ P1-1 评论任务状态不校验枚举，非法值落库污染状态机
- **模块**：获客域/comments
- **问题**：`PATCH /api/comments/tasks/{id} {"status":"bogus"}` → 200，任意字符串落库。`CommentTaskUpdate.status` 是裸 `str`。
- **影响**：状态机被污染，前端筛选器无法归类，产生孤儿数据
- **修复方向**：status 改为 `Literal["pending","replied","cancelled","failed"]`

#### ✅ P1-2 账号 limit_status 不校验枚举，风控状态失效
- **模块**：获客域/accounts
- **问题**：`PATCH /api/accounts/{id} {"limit_status":"bogus_status"}` → 200 落库。限流状态机（healthy/warn/throttled40/safe_mode/banned）接受任意字符串。
- **影响**：风控状态失去意义，账号健康度判断错误
- **修复方向**：limit_status 改为 `AccountLimitStatus` 枚举 Literal

#### ✅ P1-3 话术 category 不校验枚举
- **模块**：获客域/scripts
- **问题**：`POST /api/scripts {"name":"x","category":"bogus"}` → 200 落库。未限定契约的5类。
- **影响**：话术分类混乱，筛选失效
- **修复方向**：category 改为 `ScriptCategory` 枚举 Literal

#### ✅ P1-4 采集任务删除不存在 ID 返回 204 而非 404
- **模块**：获客域/crawl
- **问题**：`DELETE /api/crawl/tasks/nope_zzz` → 204 空响应。`service.delete_task` 对不存在 ID 静默 no-op。
- **影响**：调用方无法区分"删成功"与"本来就不存在"
- **修复方向**：delete_task 对不存在 ID 抛 KeyError，路由捕获返回 404

#### ✅ P1-5 线索9态状态机无流转守卫，可任意跳转/复活
- **模块**：获客域/leads
- **问题**：`PATCH /leads/{id}` 可 collected→deal_won 直跳，可 rejected→sent 复活。且手动 PATCH status→wechat_added 时不写 wechat_added_at。
- **影响**：状态数据可随意篡改，转化漏斗数据失真
- **修复方向**：建立状态机流转矩阵，PATCH status 时校验源→目标合法性；终态（rejected/deal_won）不可回退

#### ✅ P1-6 私信 simulate 同昵称不去重，重复创建线索/会话
- **模块**：获客域/dm-inbox
- **问题**：同昵称连续调用 `POST /dm/inbox/simulate` 每次都新建会话+新线索。契约要求 get-or-create。
- **影响**：线索池去重失效，同一用户产生重复线索，私信承接闭环断裂
- **修复方向**：simulate 按 nickname 查找已有会话/线索，存在则复用（追加消息），不存在才新建

#### ✅ P1-7 话术启用/禁用端点缺失
- **模块**：获客域/scripts
- **问题**：`PATCH /api/scripts/{id}` → 404。无法停用话术、改名称/分类。变体级 switched_off 可改，但话术级 active 无入口。
- **影响**：无法管理话术上下架
- **修复方向**：增加 `PATCH /api/scripts/{id}` 支持 active/name/category 更新

#### ✅ P1-8 漏斗数据与数据库不一致
- **模块**：分析域/dashboard
- **问题**：近30天对比：collected API=15/DB=23，wechat API=3/DB=6，deal API=0/DB=1。
- **影响**：漏斗看板数据不可信，运营决策依据错误
- **修复方向**：核查 funnel 查询的时间窗口过滤条件、状态映射逻辑，确保与 leads 表实际数据一致

#### ✅ P1-9 归因数据与数据库不一致
- **模块**：分析域/dashboard
- **问题**：own_comment API=12/DB=17，referral 渠道在归因中完全缺失（DB有1条）。
- **影响**：渠道ROI分析错误
- **修复方向**：核查 attribution 查询的 source 枚举覆盖和时间窗口

#### ✅ P1-10 负数成交金额未校验，脏数据入库
- **模块**：客户域
- **问题**：`POST /customers/{id}/deal {"amount":-100}` → 200 入库。导致漏斗成交数与工作台不一致（漏斗可能过滤负金额，工作台统计全部）。
- **影响**：脏数据影响统计，漏斗与工作台数据矛盾
- **修复方向**：`DealInput.amount` 加 `gt=0` 校验；已有负金额数据需清洗

#### ✅ P1-11 客户阶段回退无状态机保护
- **模块**：客户域
- **问题**：对已 won 的客户 `POST /stage {"stage":"measured"}` → 200，阶段被回退。
- **影响**：阶段数据可随意篡改，业务流程不严谨
- **修复方向**：建立客户阶段流转矩阵，终态（won/lost）不可回退，非终态仅可前进或按规则回退

#### ✅ P1-12 客户响应缺失 lostReason/lostAt 字段
- **模块**：客户域
- **问题**：流失客户详情不返回 lostReason/lostAt（DB中有数据但 API 不返回）。
- **影响**：前端无法展示流失原因，流失分析缺失
- **修复方向**：CustomerRead schema 增加 lostReason/lostAt 字段

#### ✅ P1-13 跟进创建时 customerId/customerName 被忽略
- **模块**：客户域/followups
- **问题**：`POST /followups` 传入 customerId/customerName，返回 `customerId:null, customerName:""`。
- **影响**：跟进任务无法关联客户
- **修复方向**：FollowUpCreate schema 增加 customerId/customerName 字段并持久化

#### ✅ P1-14 客户无更新端点（PATCH）
- **模块**：客户域
- **问题**：`PATCH /api/customers/{id}` → 405。无法修改客户信息（名称、预估金额、下一步动作等）。
- **影响**：客户信息一旦创建无法编辑
- **修复方向**：增加 `PATCH /api/customers/{id}` 端点

#### P1-15 ✅ AI 双设置接口 split-brain，配置不同步
- **模块**：系统能力/ai
- **问题**：`PUT /api/ai/settings` 只改内存不落库，重启后丢失；与 `PUT /api/settings/ai`（写DB+reload）数据不一致。
- **影响**：AI 配置改了不生效或重启丢失，双 Provider 不可用
- **修复方向**：统一为一个写入口（/settings/ai），/ai/settings 的 PUT 改为调用 SettingsService；或 /ai/settings 也写 DB 并触发 reload

#### P1-16 ✅ Provider 切换后模型名错配
- **模块**：系统能力/ai
- **问题**：provider=doubao 时返回 model="qwen-plus"（DashScope 模型名），配 Key 后会调错模型接口。
- **影响**：AI 生成功能配置后必然失败
- **修复方向**：切换 provider 时自动重置为该 provider 的默认模型名；或 model 字段按 provider 分组校验

#### P1-17 ✅ api_key 明文泄露
- **模块**：系统能力/settings + backup
- **问题**：`GET /api/settings/ai`、`GET /api/settings`、`POST /backup/export-json` 明文返回 api_key。对比 `/api/ai/settings` 正确返回 masked_key。
- **影响**：密钥安全隐患，备份文件含明文密钥
- **修复方向**：所有 settings 读接口对 api_key 脱敏（masked_key），仅写接口接受明文；backup 导出时脱敏或加密

#### P1-18 ✅ 通知开关不生效，形同虚设
- **模块**：系统能力/notifications
- **问题**：`NotificationService.create()` 不读取 new_dm_alert/followup_reminder/deal_alert 开关；各业务点无条件调用 create。关闭 deal_alert 后成交仍发通知。
- **影响**：用户无法控制通知频率
- **修复方向**：create() 中按通知类型读取对应开关，关闭时跳过

#### P1-19 ✅ dm 发送失败/频控不触发通知
- **模块**：系统能力/dm-sender
- **问题**：DmSenderService 未注入 notification_service，send_one 失败/need_captcha/throttled 只写 dm_send_results，不发通知。
- **影响**：发送失败无告警，用户不知情
- **修复方向**：注入 notification_service，发送失败/频控时创建通知

#### ✅ P1-20 前端字段名不匹配导致显示 undefined（2处）
- **模块**：前端
- **问题**：
  - 跟进列表：前端用 `f.customer`，后端返回 `customerName` → 客户名显示 undefined
  - 归因页：前端用 `v.title`，后端返回 `video` → 视频标题显示 undefined
- **影响**：关键页面数据显示异常
- **修复方向**：统一字段命名（建议后端统一 camelCase），或前端适配

---

### P2 — 体验问题（20项）

| # | 模块 | 问题 | 修复方向 |
|---|------|------|----------|
| ✅ P2-1 | 获客域/dm | `GET /dm/queue?status=bogus` 返回全量，其他列表非法值返回空，行为不一致 | 统一为返回空列表或422 |
| ✅ P2-2 | 获客域/accounts | factors 局部更新覆盖手动设置的 limit_status（partial update 语义错误） | factors 更新不重置 limit_status |
| ✅ P2-3 | 获客域/leads | score 无范围约束，允许负数（-5 落库） | 加 `ge=0, le=100` |
| ✅ P2-4 | 获客域/leads | 无批量操作端点（batch-update 405）、无删除端点（DELETE 405） | 增加批量改状态/删除 |
| ✅ P2-5 | 获客域/scripts | 话术库零预置数据（0条），发送主流程缺话术支撑 | 初始化种子话术或引导创建 |
| ✅ P2-6 | 系统能力/monitor | monitor/tasks scheduler.status 在 running/stopped 间跳动，监控失真 | 统一状态来源，加缓存 |
| ✅ P2-7 | 系统能力/notifications | 标记不存在通知已读返回 200（应404） | 不存在ID返回404 |
| ✅ P2-8 | 系统能力/ai | 非法 provider 被静默忽略（应422） | provider 加枚举校验 |
| ✅ P2-9 | 系统能力/数据 | 历史数据 `????` 乱码（账号昵称/线索/通知），历史写入编码错误 | 数据清洗脚本 + 修复写入编码 |
| ✅ P2-10 | 系统能力/monitor | 无 CPU/内存资源指标，monitor/health 仅 backend/db/aiConfig | 增加 psutil 采集 cpuPercent/memoryPercent |
| ✅ P2-11 | 系统能力/reports | 缺日报端点 `/reports/daily`（404） | 增加 daily 报表 |
| ✅ P2-12 | 系统能力/dm-sender | dm 发送失败无自动重试，仅暂停账号1小时 | 增加重试队列（指数退避） |
| ✅ P2-13 | 系统能力/backup | 备份恢复无二次确认，一步即覆盖生产库 | 增加确认 token 机制 |
| ✅ P2-14 | 前端/health | 账号健康页 avatarHue 字段缺失，头像背景色 `hsl(undefined...)` | 后端补充 avatarHue 或前端加默认值 |
| ✅ P2-15 | 前端/workbench | 工作台 alerts 始终为空，无预警规则生成 | 后端根据低健康分/发送失败动态生成 alerts |
| ✅ P2-16 | 前端/followups | 今日跟进列表为空，无自动 SOP 生成，跟进页无新建按钮 | 客户阶段变更时自动生成 followup；前端加新建按钮 |
| ✅ P2-17 | 客户域 | 创建客户 estValue 用 camelCase 时被忽略（schema 为 est_value 无 alias_generator） | CustomerCreate 加 alias_generator=to_camel |
| ✅ P2-18 | 客户域/followups | 无跟进延期端点 `POST /followups/{id}/delay`（404）、无编辑端点 PATCH | 增加延期/编辑端点 |
| ✅ P2-19 | 客户域 | 无客户删除端点 DELETE（405）；归因缺话术/账号维度（byScript/byAccount） | 增加删除端点；归因增加多维度 |
| ✅ P2-20 | 客户域/onboarding | 无 onboarding 状态查询/跳过端点（404）；safe-mode/enter 和 wizard 空body不校验（应422） | 补全端点；空body加校验 |

---

### P3 — 优化建议（15项）

| # | 模块 | 问题 | 建议 |
|---|------|------|------|
| ✅ P3-1 | 全局 | snake_case/camelCase 命名风格不统一（leads/inbox/crawl 用 snake，accounts/comments/scripts 用 camel） | ✅ 已修复：leads/dm_inbox schema 加 `_Camel` 基类（alias_generator=to_camel + populate_by_name=True），输出统一 camelCase，输入兼容 snake_case/camelCase；crawl 保持 snake_case 输出（前端 api-crawl.js 直接读取，不经过 _normalize） |
| ✅ P3-2 | 获客域/dm-inbox | 回复纯空白内容未校验（`"   "` 落库） | ✅ 已修复：`ReplyRequest.content` 加 field_validator，strip（含全角空格）后为空返回 422 |
| ✅ P3-3 | 获客域/dm-inbox | mark-wechat 不幂等，重复调用仍返回 ok | ✅ 已修复：已加微时返回 200 + already:true/message，不重复关会话/建客户/写事件 |
| ✅ P3-4 | 获客域/leads | 读侧非法过滤值不报错（?status=bogus 返回空列表） | ✅ 已确认行为（读侧宽松设计）：非法 status 精确匹配命中 0 条返回空列表，不报错；写侧仍有枚举校验 |
| ✅ P3-5 | 获客域/crawl | 采集 start/stop 重复调用无防护（已完成任务可重置重跑） | ✅ 已修复：终态(completed/failed/cancelled) start 返回 409；running 幂等；pending 可启动 |
| ✅ P3-6 | 获客域/comments | 无单条评论任务详情 GET（405） | ✅ 已修复：新增 `GET /comments/tasks/{id}` 返回任务详情，不存在 404 |
| ✅ P3-7 | 系统能力/export | 无导出进度端点（当前同步导出，影响小） | 异步导出时增加进度查询 |
| ✅ P3-8 | 系统能力 | scheduler 日限80 vs dm_sender 日限50，口径不统一 | 统一日频上限配置 |
| ✅ P3-9 | 系统能力/dm-sender | send-batch 空队列提示 message 被 response_model 丢弃 | BatchSendResponse 增加 message 字段 |
| ✅ P3-10 | 系统能力/dm-sender | minInterval>maxInterval 被静默归一化（应400） | 显式校验 min<=max |
| ✅ P3-11 | 系统能力/monitor | desktop 健康度恒为"未接入" | 接入桌面端心跳 |
| ✅ P3-12 | 前端 | 侧边栏导航角标数字硬编码（20/9/7），与真实数据不符 | ✅ 已修复：index.html 硬编码值 20/9/7 改为 0；新增 `GET /api/workbench/badges` 汇总端点返回 {leads, dmQueue, accountsWarn, customers, followups, unreadNotifications}；前端 `renderNavBadges()` 改用 badges 端点，新增 `getNavBadges` API 函数 |
| ✅ P3-13 | 客户域/compliance | 安全模式中 resume 返回200+ok:false（应4xx） | 改为 409 Conflict |
| ✅ P3-14 | 分析域 | 漏斗 cost 数据全为0（无 spend 数据接入） | ✅ 已修复：新增 `ad_spend` 表（id/date/platform/amount/note/created_at）；新增 `POST /api/analytics/spend` 和 `GET /api/analytics/spend` 端点；funnel_stats 从 ad_spend 表读取花费计算 totalSpend/perLead/perWechatAdd/perDeal；FunnelResponse 增加 `spendConnected` 字段标注是否接入 |
| ✅ P3-15 | 获客域/crawl | 采集任务中文名称存储后返回乱码 | ✅ 已确认：SQLite 连接与 FastAPI 均为 UTF-8，新建中文名称任务存取正常（无 ????）；历史乱码数据由 P2-9 clean_garbled.py 清洗 |

---

### 测试覆盖说明

| 测试域 | 测试端点数 | 通过 | 失败/缺失 | 子智能体 |
|--------|-----------|------|-----------|----------|
| 获客域（leads/comments/dm/scripts/accounts/crawl） | 33 | 24 | 9 | A |
| 客户域+分析域+工作台（customers/followups/dashboard/compliance/onboarding） | 36 | 27 | 9 | B |
| 系统能力（ai/settings/scheduler/export/reports/notifications/backup/monitor/dm-sender） | 40+ | 30+ | 10+ | C |
| 前端页面+流程闭环（20页面+E2E流程） | 20页面+55 API | 16页面正常 | 4页面异常 | D |

**端到端流程验证**：创建采集任务→导入线索→创建评论任务→标记回复→模拟私信接收→标记加微→手动创建客户→阶段推进→成交→工作台数据更新，除 P0-2（加微不自动建客户）和 P0-4（collected无法发私信）外，其余步骤均成功。
