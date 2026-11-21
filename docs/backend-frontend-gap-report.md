# 前后端连通性 + 数据库体检报告

> ⚠️ 本文件的**明细章节**是第一轮探测快照（2026-11-14 上午），第二节矩阵已按 13:20 复跑刷新。
> 用户级结论请看同目录 `acceptance-report.md`（四关验收，真能用 68 件 / 假成功 0 件）。
> 第三节列出的通道多数已按四关验收逐批补齐，仅作历史对照。

> 生成时间：2026-11-14（矩阵于 13:20 复跑刷新）
> 方法：从壳编译产物提取通道名 → 与后端注册表求交 → **逐条实调**（真库副本，零污染）→ 数据库结构与数据一致性只读体检
> 复跑命令：`cd backend && .venv/Scripts/python.exe tools/probe_channels.py` / `tools/probe_db.py`

## 一、安全边界

- 探测前把真库复制到临时目录，`DATA_DIR` / `LEADS_DB_PATH` 指向副本，**247 个通道全部在副本上调用**
- 会真起线程 / 改授权 / 装更新 / 清库的 18 个通道只做静态判定，不实际调用
- 数据库体检全程 `mode=ro` 打开，不写一字节

## 二、总矩阵

| 判定 | 数量 | 含义 |
|---|---|---|
| ok | 104 | 有真实数据返回（其中前端会调用的 79 条） |
| empty | 16 | 正常空态/无数据源，返回结构完整 |
| disabled | 95 | 明确返回「功能未开通」（其中 103 条是**前端会调但后端没登记**，落到兜底） |
| error | 14 | 空参调用触发必填校验（正常契约，非缺陷） |
| exception / http_error | 0 | **无 500、无未捕获异常** |
| skipped | 18 | 危险通道，未实际调用 |

- 前端会调用的通道：**217 个**
- 后端登记通道：**144 个**（显式实现 86 / 真实本地数据或中性空态 43 / 门禁未开通 3 / 窗口适配 12）
- **前端会调但后端没登记：103 个**（比第一轮的 124 条少，剩余集中在批量跟进、清日志、账号数据采集）
- 后端登记但前端不调用：30 个（运维/授权/兼容别名，正常）

**关键判断：没有任何「已登记通道运行时崩溃」。** 缺口集中在「没登记」而非「登记了跑不通」——这意味着补通道是纯增量，不会牵动现有链路。

## 三、124 个未接通，按模块

### 评论获客（9）
`save-entity-leadgen-task`、`update-entity-leadgen-task`、`delete-entity-leadgen-tasks`、
`start-entity-leadgen-task-runtime`、`stop-entity-leadgen-task-runtime`、
`delete-entity-leadgen-lead-records`、`delete-comment-visibility-related-records`、
`export-comment-visibility-details`、`get-comment-visibility-candidates`

> ⚠️ **P0**：`save/update` 未接 → 界面改完配置点保存提示成功但不落库；
> `start/stop-*-runtime` 未接 → 任务「运行中」状态靠 start-task 单边维持，壳的运行时同步拿不到确认。

### 关键词获客（2）
`delete-leadgen-tasks`、`get-leadgen-task-hashtag-videos`

### 线索池 / 历史 / 采集库（20）
`get-history`、`delete-leads-from-history`、`update-lead-in-history`、`update-leads-intention`、
`delete-low-intention-leads`、`delete-touched-leads`、`delete-self-warmup-leads`、
`export-leads`、`save-lead-export-csvs`、`export-collected-library`、
`get-collected-videos-page`、`get-collected-authors-page`、
`get-video-main-comments`、`clear-video-main-comments`、`clear-all-memory`、
`get-processed-videos-page`、`import-collected-authors-to-leads`、
`check-processed-video-urls`、`export-batch-unit`、`batch-accounts`

> ⚠️ `get-history` 是线索池「历史任务分组视图」的数据源，未接 → 该视图永远空。

### 批量跟进（13）
`get-batch-follow-tasks`、`get-batch-follow-runs-page`、`get-batch-follow-run-details-page`、
`get-batch-follow-user-history`、`save-batch-follow-task`、`clear-batch-follow-runs`、
`clear-batch-follow-run-logs`、`delete-batch-follow-runs`、`cancel-batch-follow-user-history`、
`delete-failed-batch-follow-leads`、`test-batch-follow-proxy`、`batch-follow-tasks`、`batch-follow-history`

> 整组未接：批量跟进是完整功能模块，页面全在、后端零实现。

### 监控任务（12）
`save-monitor-task`、`delete-monitor-tasks`、`start-monitor-task-runtime`、`stop-monitor-task-runtime`、
`stop-monitor`、`stop-monitor-manual-retry`、`clear-monitor-task-runtime-logs`、`clear-monitor-comment-seen`、
`delete-monitor-match-records`、`get-monitor-task-cycles`、`get-monitor-task-video-comments`、
`get-monitor-live-state`、`get-monitor-live-events`、`get-monitor-group-history`、`get-monitor-group-list-cache`、`open-profile`

### 养号 / SelfWarmup（14）
`save-self-warmup-task`、`delete-self-warmup-tasks`、`stop-self-warmup-task`、`stop-self-warmup-tasks-runtime`、
`start-self-warmup-on-app-ready`、`cancel-self-warmup-startup`、`stop-self-warmup-manual-retry`、
`clear-self-warmup-task-logs`、`clear-self-warmup-event-category`、`clear-self-warmup-user-rounds`、
`delete-self-warmup-events`、`get-self-warmup-account-stats`、`get-self-warmup-user-stats`、`runtime-sync`

### 作品发布（7）
`save-work-publish-task`、`delete-work-publish-task`、`delete-work-publish-tasks`、
`save-work-publish-template`、`delete-work-publish-template`、
`start-work-publish-task-runtime`、`stop-work-publish-task-runtime`

### 账号池（4）
`get-license-entitlement`、`export-douyin-cookie-session`、`import-douyin-cookie-session`、`test-account-proxy`

> 涉及授权额度与登录态导入导出，属高敏感，需先定策略（写入磁盘的 cookie 如何加密、谁能调用）。

### 系统设置（8）
`get/set-hardware-acceleration`、`get/set-ui-zoom-factor`、`get/set-use-system-proxy`、
`get/set-auto-cache-cleanup`、`get/set-manual-link-open-mode`、`set-all-views-muted`

### 更新 / 公告（2）
`check-update`（含安装包下载/静默安装链路）、`refresh-system-announcements`、`release-automation-view`

### 大屏批量（2）
`stop-batch-account`、`stop-batch-action`

### 账号数据 / 定时（3）
`run-account-data-collect`、`save-account-data-schedule`、`resume-nurture-safety`、
`save-leadgen-schedules`、`save-entity-leadgen-schedules`、`save-monitor-schedules`、
`save-self-warmup-schedules`、`save-nurture-config`、`save-account-data-schedule`（定时任务保存整组）

### 帮助 / 其他（3）
`get-radar-faqs`、`submit-faq-question`、`open-profile-qr-directory`、`toggle-intention`、
`start-comment-like-reader`、`stop-comment-like-reader`

## 四、空态通道（11）

| 通道 | 说明 |
|---|---|
| `consume-entity-leadgen-live-preview` / `consume-video-monitor-live-preview` / `consume-work-publish-live-preview` | 大屏「实时预览」永远空——壳期望流式推送，后端只返回空 |
| `get-entity-leadgen-busy-account-ids` | 无任务运行时为空，正常 |
| `get-leads-by-ids` | 空参为空，正常 |
| `get-self-warmup-tasks` / `get-work-publish-tasks` | 表里无记录，正常 |
| `get-*-lease-pauses` | 无租约，正常 |
| `get-entity-leadgen-task-runtime-logs` | 从 `task_runtime_logs` 读，表未建=无日志；首次真跑后才建表 |

## 五、数据库体检

**库：`backend/data/lead_system.db`，2.57 MB，32 张表，0 视图**

### 5.1 关键表行数

| 表 | 行数 | 判断 |
|---|---|---|
| leads | 902 | 主体（清理后） |
| behavior_events | 1132 | 正常 |
| messages | 1817 | 正常 |
| tasks | 353 | 正常 |
| conversations | 268 | 正常 |
| comment_tasks | 61 | 正常 |
| followups | 59 | 正常 |
| crawl_tasks | 17 | 正常 |
| notifications | 18 | 正常 |
| system_settings | 27 | 正常 |
| **leadgen_pools** | **0** | 空表：真机任务从未在真库初始化过资源池 |
| **ad_spend / script_usage** | **0** | 历史模块，未使用 |

### 5.2 代码引用但库里不存在的表（运行到即报错）

真正缺失的业务表（排除 Python 语法噪声后）：

`task_runtime_logs`、`licenses`、`instances`、`account_quotas`、`account_risk_events`、
`audit_log`、`comment_send_log`、`dm_send_log`、`lead_candidates`、`nurture_sessions`、
`pipeline_progress`、`pipeline_runs`、`usage_daily`、`account_pool_states`

- `task_runtime_logs`：**懒创建**（`CREATE TABLE IF NOT EXISTS`），首次写日志时才建，属正常设计
- `licenses` / `instances`：走管理后台独立库（`ADMIN_DB_PATH`），不属主库，正常
- 其余（`account_quotas` / `account_risk_events` / `audit_log` / `comment_send_log` / `dm_send_log` / `lead_candidates` / `nurture_sessions` / `pipeline_*` / `usage_daily` / `account_pool_states`）：**代码里有 SQL，库里有没表**，一旦走到即 `OperationalError`。这些功能当前都处于「通道未接」状态，所以没暴露；补通道时必须一并建表。

### 5.3 数据一致性

| 检查 | 结果 |
|---|---|
| `account_events` 指向已删除账号 | **12 条孤儿** |
| `dm_send_results` 指向已删除账号 | **33 条孤儿** |
| `leads` 无评论内容 | 242 条（**全部是 9 月历史演示数据**，非采集所得） |
| `leads` 无昵称 | 0 |
| `leads` score 为空 | 0 |

**含义**：账号删了但事件/发送记录没级联清理 → 「账号数据」页的统计会把已删账号算进去，数字偏高。

### 5.4 leads 字段填充率（关键项）

| 字段 | 填充 | 说明 |
|---|---|---|
| nickname / score / status | 100% | 健康 |
| comment | 73.7% | 缺的 242 条是 9 月演示数据 |
| source_keyword | 69.6% | 同上 |
| source_url / external_id / video_id / comment_id | 47.6% / 47.6% / 40.8% / 40.8% | 老数据未回填 |
| **customer_need** | **4.7%** | 需求摘要几乎空 → 线索详情页「需求」区基本是空的 |
| **intent_category / sentiment / risk_level / insight_json** | **0%** | AI 洞察整块从未写入 |
| **account**（归属账号） | **0%** | 线索无法溯源到具体账号 → 账号维度 ROI 无法统计 |

> `leads` 表没有 `account_id` 列，归属信息存在 `account` 文本列且全部为空。
> 这意味着「按账号看线索/成交」这条分析链路目前断的。

### 5.5 leads 来源分布（清理后）

| source | 数量 | 性质 |
|---|---|---|
| own_comment | 728 | 9 月演示期数据 |
| competitor | 157 | 9 月竞品采集 |
| competitor_comment | 17 | 9 月竞品评论 |
| **keyword_comment** | **0** | **真机采集从未入库成功过** |

## 六、本轮已修

### 1）关闭两个假数据注入端点（真库污染的真正源头）

`POST /api/demo/seed-report-data` 一次写 42 条「日报客户N」；`POST /api/crawler/import-demo` 写硬编码 `demo-user`。
两者默认在生产路径上敞开。新增 `app/core/demo_guard.py`：**默认 403**，只有设 `ALLOW_DEMO_SEED=1` 才放行。

### 2）清理 19 条测试残留线索

`装修-1/2/3`、`日报客户1/31`、`demo-user-1/2`、`probe-user`、`confirm-user`、`file-user-*`、
`filter-user`、`draft-user`、`flow-user`、`detail-user`、`jsonl-user`、`business-flow-user`、`update-user`。
全部为 2026-11-13 集中写入（真机入库数为 0，可确定非采集所得）。
备份：`backend/data/_backups/lead_system_before_purge_20261114.db`；leads 921 → 902。

### 3）测试隔离补完（桥接侧）

`tests/conftest.py` 原先只隔离 `app.main` 侧，桥接侧（`app/paths.py → LEADS_DB_PATH`）仍指向真库。
现在复制真库到临时目录并整体重定向——测试读写全在副本上，只读断言照样成立。
验证：**427 passed**，跑完真库 leads 仍为 902。

## 七、建议修复顺序

| 优先级 | 内容 | 理由 |
|---|---|---|
| **P0** | 评论获客 / 关键词获客的 `save / update / delete` + `start/stop-*-runtime` | 「保存成功但不落库」会让人以为配置改了；runtime 通道缺失让壳无法确认任务真实状态 |
| **P0** | `get-history`（线索池历史分组视图） | 该视图现在是空的 |
| **P1** | 账号级数据链路：`leads.account` 回填 + 建 `account_id` 索引 | 否则账号 ROI / 按账号筛线索都做不了 |
| **P1** | 监控任务、养号、作品发布的 `start/stop-runtime` | 三个模块「页面能开、真跑没接」 |
| **P1** | 孤儿数据清理（`account_events` 12 / `dm_send_results` 33） | 账号数据页统计偏高 |
| **P2** | 批量跟进整组（13 通道） | 完整模块零实现，工作量最大 |
| **P2** | 系统设置 / 更新 / 采集库 | 要么接，要么在 UI 上标灰 |
| **暂缓** | cookie 导入导出、`get-license-entitlement` | 涉及账号安全与授权，需先定策略 |
