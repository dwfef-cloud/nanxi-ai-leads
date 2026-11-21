# 阿里山前端（壳 /ui）功能全量清单

> 生成方式：**从代码提取，不是凭记忆**。
> 1. 页面/功能面 ← 壳编译产物 `ui/assets/*.js`（97 个 js，其中 22 个是页面级 chunk）
> 2. 每个页面会调哪些通道 ← 从 bundle 里抽 `invoke("通道名")` 字面量，与后端通道表求交
> 3. 通道状态 ← `backend/app/api/ipc_bridge.py` 的 EXPLICIT / EMPTY_CHANNELS / BLOCKED_CHANNELS
> 4. 未登记通道的行为 ← 实测（返回 `{success:false, msg:"功能未开通"}`，不造假数据）
>
> 口径：`ui/` 是壳的编译产物，不可改；能改的只有桥接层（后端通道）。

## 一、总览

| 指标 | 数量 |
|---|---|
| 壳 js 产物 | 97（22 个页面级 chunk + 主壳 index + 工具/图标/样式） |
| 后端已登记通道 | 116（已接真数据 62 / 返回空态 42 / 明确未开通 12） |
| 前端实际调用到的已登记通道 | 104 |
| 前端调用但后端未登记（= 点了提示「功能未开通」） | **约 70 个**（已剔除样式类名与原生视图通道） |
| 后端登记但前端没调用 | 12 |

**一句话结论**：壳的功能面比当前后端大得多——22 个页面里，**线索池、评论获客、关键词获客、大屏**基本接通；**监控任务、养号、作品发布**是"页面能开、真跑没接"；**系统设置、更新、批量跟进、采集库**整块只有前端入口。

## 二、菜单/页面 → 接通状态

| 页面（chunk） | 产品名 | 通道数 | 状态 |
|---|---|---|---|
| `index` | 主壳（导航/授权弹窗/更新/全局状态） | 53 | 混合：29 已接 / 15 空态 / 9 未开通 |
| `Dashboard` | 大屏 | 7 | 6 项采集预览为空态，`stop-task` 已接 |
| `AccountManager` | 账号池 | 0 | **已登记通道 0**：额度、cookie 导入导出、代理测试全未接（账号读写走主壳通道） |
| `AccountData` | 账号数据 | 3 | 看板已接，**采集/定时未开通** |
| `AccountNurture` | 账号养号 | 2 | 仅复用 `start-task`/`stop-task` |
| `LeadgenTasks` | 关键词获客 | 8 | 主链路已接，**删除任务/话题视频未接** |
| `EntityLeadgenTasks` | 评论获客 | 8 | 主链路已接，**保存/更新/删除未接** |
| `CommentLikeTasks` | 评论点赞任务 | 4 | 只有日志/任务读取，**启动/停止未接** |
| `MonitorTasks` | 监控任务 | 8 | 3 已接 / 5 空态，**保存/更新/删除/实时流未接** |
| `SelfWarmup` | 养号 | 9 | 2 已接 / 7 空态，**增删/日志/统计未接** |
| `WorkPublish` | 作品发布 | 4 | 只有额度/任务读取，**模板与增删未接** |
| `LeadPool` | 线索池 | 8 | 分页/筛选/统计已接；**历史、导出、采集库、批量跟进未接** |
| `LeadDetailModal` | 线索详情 | 0 | 更新线索（`update-lead-in-history`）未接 |
| `MessageCenter` | 消息中心 | 3 | 未读数已接，**会话/通知配置未接** |
| `AiPersonas` | 智能体 | 4 | 全接（增删改 + 试测额度 + 试跑） |
| `AuthModal` | 激活授权 | 2 | 校验已接（真实激活走 `activate`） |
| `Settings` / `SystemSettings` | 设置 | 0 / 0 | 系统级设置项**一个都没接** |
| `Faq` / `SupportChatModal` | 帮助/客服 | 0 / 0 | 后端未登记 |
| `CommentVisibilitySamplePicker` | 评论可见性取样 | 0 | 后端未登记 |
| `CreatorAggregate` | 达人数据 | 0 | 仅原生视图，无后端通道 |
| `ProfileShareQrOption` | 主页分享二维码 | 0 | 纯前端 |

## 三、已接通的真数据能力（62 个通道）

- **线索池**：分页/筛选/统计/按 id 批量取、`query-leads-for-batch`、已处理视频
- **关键词获客 / 评论获客**：任务列表与详情、任务线索、运行日志、并发上限、账号占用
- **任务执行**：`start-task` / `stop-task`（真机采集已通：搜索→翻视频→读评论→入库→打分→推进）
- **资源池**：关键词/话题/指定视频三类池的 init / claim / release
- **账号池**：读写账号、账号数据看板
- **智能体**：人设增删、任务配置存取、试测额度、AI 试跑
- **授权**：`check-auth` / `refresh-auth` / `activate` / `license-status` / HMAC 复核
- **系统**：版本、公告、性能、运行时状态、任务配置

## 四、返回空态（42 个通道）——页面不报错，但没有内容

主要集中在 **监控任务 / 养号 / 作品发布 / 大屏实时预览 / 会话通知配置**：
`get-monitor-*`、`get-self-warmup-*`、`get-work-publish-*`、`consume-*-live-preview`、
`get-chat-notification-config`、`get-*-storage-status`、`get-*-schedules`、
`get-active-automation-tasks`、`get-batch-runtime-state`、`support-bootstrap`。

## 五、明确标记「未开通」（12 个）

`generate`、`run-account-data-collect`、`save-account-data-schedule`、
`save-entity-leadgen-schedules`、`save-leadgen-schedules`、`save-monitor-schedules`、
`save-nurture-config`、`save-self-warmup-schedules`、`save-self-warmup-task`、
`start-self-warmup-task-runtime`、`stop-monitor`、`update-self-warmup-task`

## 六、前端有、后端没接的功能（按模块，约 70 项）

> 点击后统一提示「功能未开通」。原生窗口类通道（`open/destroy -creator-view` 等）由桌面壳自己处理，不在缺口内。

### 评论获客
`save-entity-leadgen-task`、`update-entity-leadgen-task`、`delete-entity-leadgen-tasks`、
`start-entity-leadgen-task-runtime`、`stop-entity-leadgen-task-runtime`、
`delete-entity-leadgen-lead-records`、`delete-comment-visibility-related-records`、
`export-comment-visibility-details`、`get-comment-visibility-candidates`

### 关键词获客
`delete-leadgen-tasks`、`get-leadgen-task-hashtag-videos`

### 监控任务（缺口最大）
`save-monitor-task`、`update-monitor-task`、`delete-monitor-tasks`、
`start-monitor-task-runtime`、`stop-monitor-task-runtime`、`stop-monitor-manual-retry`、
`clear-monitor-task-runtime-logs`、`clear-monitor-comment-seen`、`delete-monitor-match-records`、
`get-monitor-task-cycles`、`get-monitor-task-video-comments`、`get-monitor-live-state`、
`get-monitor-live-events`、`get-monitor-group-history`、`get-monitor-group-list-cache`、
`list-collected-video-import-keys`

### 养号（SelfWarmup）
`delete-self-warmup-tasks`、`stop-self-warmup-task`、`stop-self-warmup-tasks-runtime`、
`stop-self-warmup-manual-retry`、`start-self-warmup-on-app-ready`、`cancel-self-warmup-startup`、
`clear-self-warmup-task-logs`、`clear-self-warmup-events`、`clear-self-warmup-user-rounds`、
`delete-self-warmup-events`、`get-self-warmup-account-stats`、`get-self-warmup-user-stats`、`runtime-sync`

### 作品发布
`save-work-publish-task`、`update-work-publish-task`、`delete-work-publish-task`、
`delete-work-publish-tasks`、`save-work-publish-template`、`delete-work-publish-template`、
`start-work-publish-task-runtime`、`stop-work-publish-task-runtime`

### 线索池 / 采集库 / 批量跟进
`get-history`、`delete-leads-from-history`、`update-lead-in-history`、`update-leads-intention`、
`delete-low-intention-leads`、`delete-touched-leads`、`delete-self-warmup-leads`、
`export-leads`、`save-lead-export-csvs`、`export-collected-library`、`clear-collected-library`、
`get-collected-videos-page`、`get-collected-authors-page`、`get-video-main-comments`、
`clear-video-main-comments`、`clear-all-history`、`get-processed-videos-page`、`claim-next`、
`start-batch-action`、`open-in-account`
以及批量跟进整组：`get-batch-follow-tasks`、`get-batch-follow-runs-page`、
`get-batch-follow-run-details-page`、`save-batch-follow-task`、`clear-batch-follow-runs`、
`clear-batch-follow-run-logs`、`delete-batch-follow-runs`、`cancel-batch-follow-user-history`、
`delete-failed-batch-follow-leads`、`test-batch-follow-proxy`

### 账号池
`get-license-entitlement`、`export-douyin-cookie-session`、`import-douyin-cookie-session`、`test-account-proxy`

### 消息中心
`set-message-center-session`、`test-chat-message-webhook`

### 系统设置（整块未接）
`get/set-hardware-acceleration`、`get/set-ui-zoom-factor`、`get/set-use-system-proxy`、
`get/set-auto-cache-cleanup`、`get/set-manual-link-open-mode`、`get-cache-size`、`clear-cache`、
`set-all-views-muted`

### 更新与公告
`check-update`、`apply-update-and-exit`、`start-download`、`run-installer`、`refresh-system-announcements`

### 大屏
`stop-batch-account`、`stop-batch-action`、`release-automation-view`

### 帮助
`get-radar-faqs`、`submit-faq-question`

## 七、后端登记但前端没调用（12 个）

`get-app-info`、`get-device-id`、`app-version`、`get-concurrency-limit`、`get-creator-aggregate`、
`get-message-aggregate`、`issue-demo-license-code`、`license-reset`、`license-revoke`、
`license-status`、`activate`、`generate`

→ 这些是给后台/运维/命令行用的（`activate`、`license-*` 走后台页与管理工具），
不属前端页面功能。

## 八、优先级建议

1. **P0 补齐会直接误导用户的操作**：评论获客/关键词获客的「保存、更新、删除」——
   现在能在界面改配置但保存不落库，用户会以为改了。
2. **P1 让真实执行闭环**：`start/stop-*-task-runtime` 系列（监控、养号、作品发布）。
   目前只有评论获客/关键词获客能真跑。
3. **P2 系统性补全**：系统设置、更新、采集库、批量跟进——这些是"看起来有、实际全空"，
   建议要么接、要么在 UI 上标灰（现在点了只提示"功能未开通"，体验可接受但不透明）。
4. **不建议做**：`get-license-entitlement`、cookie 导入导出这类高敏感能力，涉及授权与账号安全，
   要先定策略再接。
