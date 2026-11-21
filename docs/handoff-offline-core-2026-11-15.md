# 评论获客与养号：离线实现交接（2026-11-15）

> 后续真机阶段见 [桌面真机验收交接](handoff-live-acceptance-2026-11-15.md)。本文“仅离线”等描述属于真机阶段之前的历史事实；以新交接区分实际通过、回读确认及未验收部分。当前真实写操作已恢复默认关闭。

## 本轮范围

按用户最新确认，优先完成评论获客与养号的离线实现和模拟测试。真实发表评论、回复、私信、收藏和复制链接保持关闭；本轮不改作品发布。“转发”仅指复制视频分享链接，不发布站内动态。

此记录更新 `handoff-nurture-like-2026-11-14.md` 中相关功能的状态。旧记录中的真机启动、任务与账号示例不构成本轮执行授权。

本轮后续顺序已执行：先做桌面可见离线入口和完整验收，再做公开页面只读结构预检；真实平台写操作仍保持关闭。

## 已实现及接入边界

| 功能 | 本轮实现 | 当前边界 |
| --- | --- | --- |
| 发表评论 | 桌面离线执行模块完成页面与账号核对、输入框定位、草稿保护、输入核对、提交与回执核对 | 仅注入假适配器，不连接真实 BrowserWindow |
| 评论获客任务 | 离线搜索、视频去重、生成文案、主评、冷却、回复/私信计划串联 | 独立离线编排，未接入生产 `task_runner.start()` 或 IPC 入口 |
| 回复 | 桌面离线流程选择指定评论、核对回复目标、保护草稿，并同时核对请求目标、响应目标与回复内容 | 使用假 DOM 契约，真实平台页面选择器和发送链路未接入 |
| 私信 | 桌面离线流程核对会话收件人、保护草稿，并以新消息 ID、收件人、内容、发出方向及 sent 状态确认 | 使用假 DOM 契约；历史消息、收到的消息和 pending 气泡不算成功，真实发送未接入 |
| 收藏 | 桌面离线模块识别已收藏，验证状态稳定变化，防止重复点击取消收藏 | 仅离线模拟 |
| 复制链接 | 校验当前视频，生成规范链接，写入并读回假剪贴板 | 未操作系统剪贴板，仅计模拟复制数 |
| 养号结果统计 | 明确成功状态才计数；不确定结果停止后续动作，复制使用 `links_copied` | 没有执行真实养号或平台验收 |
| 点赞回执 | 严格校验 HTTP 与平台回执；删除仅发出点击就加成功数的分支 | 本轮没有验证真实平台点赞，也没有重做完整真机时长验收 |
| 桌面离线面板 | 独立 Electron 窗口 `/ui/offline.html`，可选择完整流程/评论获客/养号、正常/不确定/拒绝样例，支持启动、停止、历史记录和重启后恢复 | 仅固定虚拟账号和样例，不进入真实任务队列 |
| 公开页面只读预检 | 使用临时、未登录 Electron 会话访问公开页面，仅采集 DOM 结构并拦截非 GET/HEAD 请求 | 未登录页面没有评论、回复、私信或收藏控件，不能据此完成登录态适配 |

## 防重复与结果处理

- 同账号并发保护，按动作和规范目标去重；修改视频查询参数或文案不能绕过去重。
- 提交后缺少可靠回执、回执不匹配或异常，记录为 uncertain 并阻止重试，不能算成功。
- SQLite 原子占位支持成功/不确定状态跨进程重启去重；reservation token 防止迟到结果覆盖新尝试。
- 离线与真实 namespace 分离，离线模拟器拒绝使用真实 namespace。测试仅使用临时数据库，未迁移生产数据库。
- 真实任务统计保持零，模拟结果放在 `simulationStats` 或 `simulated*` 字段；回复数不冒充独立触达人数。
- 生成失败跳过，不使用模板冒充 AI 生成结果。
- 回复继承当前任务视频上下文，显式指定其他视频的互动会跳过；私信的失败草稿保留，清空前不会自动重发。
- 离线服务使用独立数据库 `data/offline/runs.db`，运行记录带 owner PID；桌面面板关闭后任务继续，软件重启只把真正失去 owner 的运行标为 interrupted，不自动重发。
- 第二个离线窗口不能并发启动另一条运行，但可以读取和停止现有运行；停止状态由数据库协调，避免跨窗口继续提交下一步。

## 主要文件

- `desktop/action-executor.js`：发表评论、回复、私信、收藏、复制链接的离线执行流程及真实动作拒绝门禁。
- `desktop/interaction-dom.js`：回复目标与私信会话的假 DOM 契约和探测脚本；不是已经核实的抖音页面选择器。
- `backend/app/execution/offline_service.py`、`backend/app/api/offline_runs.py`：面板后端、独立记录库、停止/重启恢复和同源门禁。
- `backend/offline_server.py`、`desktop/offline-test.js`：独立离线桌面窗口与仅离线 API 服务。
- `ui/offline.html`、`offline.css`、`offline.js`、`offline-entry.js`：可见离线测试面板和主界面入口。
- `desktop/write-commands.js`：视频 URL、评论输入与回执检查、点赞回执解析。
- `desktop/douyin_agent.js`：真实命令执行前门禁及养号点赞统计修正。
- `backend/app/runtime/browser.py`：新增动作在引擎选择和桌面入队之前直接拒绝真实执行。
- `backend/app/execution/offline_simulation.py`：离线动作模拟与结果分类。
- `backend/app/execution/offline_tasks.py`：独立评论获客离线编排。
- `backend/app/execution/persistent_gate.py`：持久化占位与去重。
- `backend/app/execution/write_safety.py`：内存占位与不确定结果保护。
- `backend/app/nurture/machine.py`：以明确状态驱动养号统计和停止。
- `desktop/_verify/offline_dom_fixture.js`、`offline_action_driver.js`：假 DOM 与跨语言测试驱动。

此前完成的复制链接文案、统计字段、停止/截止控制和外链弹窗修复应保留。仓库内有大量原有修改及未跟踪文件，不应整体还原或清理。

## 验证

最新复测通过：后端 **581 项**（36.13 秒，10 条警告），桌面离线 **27 项**；离线服务专项 **16 项**；养号四种注入脚本求值后语法检查通过。新增模块、执行器、测试驱动与桌面 agent 的 JS 语法检查通过。

跨语言测试实际覆盖 Python 离线任务 → Node 假 DOM → 桌面执行模块与注入脚本 → 假回执/消息状态 → 持久化结果，包含主评、回复、私信、收藏和复制链接。评论→回复→私信的完整离线编排及成功/不确定状态在重启后的去重已验证。

实际 Electron 窗口验收结果：五类动作各模拟成功 2 次；第二次运行被去重；中途停止后日志不再增长；回执不确定和拒绝不会计入成功；刷新页面后历史仍可查看；页面错误 0；外部请求 0。验收产物为 `desktop/_verify/offline-desktop-acceptance.json`、`offline-desktop-success.png` 和 `offline-desktop-uncertain.png`。这些结果不代表真实平台操作成功。

公开页面只读预检（临时未登录会话，2026-11-15）：导航到 `https://www.douyin.com` 成功，拦截非 GET/HEAD 请求 22 次；视频链接、评论项、回复目标、私信会话和收藏控件均为 0，发现的输入框不是评论框。`liveWritesEnabled=false`，`authenticatedFlowVerified=false`。因此真实平台适配没有通过验收，也没有启动账号登录或真实写操作。

仓库根目录下的 PowerShell 复测命令：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q --disable-warnings
node --test desktop/_verify/test_action_executor.js desktop/_verify/test_interaction_executor.js desktop/_verify/test_write_commands.js desktop/_verify/test_warmup_offline.js
node desktop/_verify/check_warmup_js.js
```

系统 `python` 是 WindowsApps 占位程序，应使用上述项目虚拟环境。

## 后续工作

1. 若推进真实平台接入，需要在用户明确授权并准备专用测试账号/目标后，用桌面壳的已登录会话只读核实实际页面结构，再为每个动作接入真实回执；不能把假 DOM 契约直接当作已验证的平台适配。
2. 如需从产品页面运行模拟，另行实现显式离线入口，保持与真实任务、真实统计和登录会话隔离。
3. 用户另行授权真机验收后，才评估生产链路接入与门禁开放；不能仅凭请求参数打开真实动作。
4. 作品发布、真实平台回执验收、完整养号真机时长复验均不属于本轮已完成内容。

本轮没有启动或重启桌面/服务，没有对真实账号发内容、收藏或复制系统剪贴板。
