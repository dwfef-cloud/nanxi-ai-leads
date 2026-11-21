# 实况大屏：评论点赞全局接入交接

日期：2026-11-15。需求：用户启动真机评论点赞却看不到实况，要求先写全局规定再开始修改。全局规定在 `global-live-dashboard-prd.md`，本轮完成其中 P0，P1/P2 保留明确待办。

## 本轮结果

确认真机任务后，桌面发送本次任务、运行和账号，根模块自动进入原有实况大屏并打开对应画面。独立点赞服务不再遗漏于大屏；卡片显示准确视频、评论作者/全文、执行账号、阶段、统计、起止时间与本次日志。

找到准确未赞评论后，执行器等待本次运行的首帧已在可见大屏绘制，最多15秒。未就绪直接结束该项且不点击、不消费原生单次尝试；UI 显示“未执行”。该条件不代替账号、准确目标、原生确认、额度和平台回执检查。

单卡与右上角“全部停止”均能停止独立评论点赞服务。单卡必须带准确 runId，旧结束卡片不能误停新运行。停止请求和最终停止分开显示，平台结果不明仍显示待核验且禁止自动重试。短任务结束后保留摘要，刷新重进可查；默认展示最近30分钟的结束摘要，历史仍在原结果库。

## 关键文件

- 后端：`backend/app/api/live_dashboard.py` 统一只读状态和准确停止；`comment_likes.py` 增加运行投影、目标与日志、停止中判断；`ipc_bridge.py` 接入新通道；`execution/live_leadgen.py` 传递观测用 runId。
- 桌面：`desktop/main.js` 原生启动后推送定位、首帧握手、画面时间查询；`comment-like.js` 首帧门禁与阶段；`douyin_agent.js` 运行绑定和截图竞态检查；`live-dashboard.js` 等待逻辑；`live-preview.js` 仅确认实际绘制且仍挂载的帧。
- UI：`LiveDashboardRuntime.js` 全局监听/恢复；`LiveDashboardModel.js` 真机投影；`Dashboard-QPICGlrw.js` 复用原卡片/视频容器与准确控制；`index.html` 加载单例；入口 bundle 的全部停止按钮状态和控制补入独立服务。`live-worker-preview.js` 返回解码绘制回执。
- 原生 `set-views-visible` 支持任务键映射到同账号窗口，避免 `entity_` 与 `douyin_` 不一致导致误隐藏。自动补建只读预览时先加载本地 viewer，避免另开平台推荐流充当现场。

三个新通道：`get-live-dashboard-state`、`stop-live-dashboard-task`、`stop-all-live-dashboard-tasks`。原生只读 `get-live-dashboard-frames` 返回按 runId 的实际绘制时间。新 runId 用于观测和停止定位，不改变原有签名授权范围。

## 验证

- Python：`test_comment_likes.py`、`test_live_acceptance_scope.py`、`test_ipc_bridge.py`、`test_task_logs.py`、`test_persistent_gate.py` 共213通过，10条既有框架警告。
- Node：评论点赞、原生启动器、预览、实况投影/首帧共26通过。
- 隔离 Electron：`comment_like_ui.js --mode=dashboard` 通过自动进入/选中、准确目标、解码首帧、取消停止、准确停止、全局停止、快速结束和刷新恢复。证据在 `desktop/_verify/live-dashboard-ui/`，`result.json` 全部通过；`native-preview.png` 是原生容器里的隔离画面。
- `--mode=real-default` 回归通过：正常入口仍为真机，原生取消不启动，离线成功不计真实。
- 改动的桌面/前端文件通过 Node 语法检查。

所有隔离测试禁止平台传输，不具备生产登录态；本轮新增真实点赞0次。两条此前 uncertain 的真实评论不得重新执行。

## 正式运行状态

约16:25正常 `Browser.close` 退出旧桌面及其后台，确认8899/9222监听均消失。带调试参数的启动命令被自动审批拒绝（未提供进一步原因）；随后使用普通桌面启动成功。当前 Electron PID24236、后台PID20184，8899正常，新状态接口业务成功、活动点赞和普通任务均为0；桌面持续轮询新接口。

普通启动恢复了默认排期线程。只读核对四类排期启用数均为0，未更改用户保存的排期配置。当前没有9222调试端口。Windows工具已识别到“阿里山AI获客”窗口，但截图两次超时；正式实例完整点击验收未完成，不能替换为隔离截图声称通过。

## 下一步

用户从桌面的评论点赞列表选择尚未尝试过的准确目标，核对原生确认后可自行验证自动打开大屏。本轮代码仍限制一个账号、一条评论、一次尝试；不能扩大门禁或自动重试此前待核验目标。

P1按全局规定统一其他现有模块与只读读取状态。新接口中的普通 `executions` 尚未取代既有前端运行流，读取会话也未投影到新大屏；不得声称全局所有功能已完成验收。
