# 评论点赞模块交接（2026-11-15）

## 当前结果

独立任务服务、界面、停止、持久化去重、详情及日志已实现。用户明确要求真机后，已用生产登录账号对其选定评论真实点击一次。平台当时返回 HTTP 200/status_code 0，页面短暂变为已赞，但独立刷新后的评论列表返回 user_digged=0、digg_count=0。因此本次最终结果为“不确定”，真实成功数 0，不能写成真机点赞通过。未发表评论、私信或作品。

独立 Electron 完整离线流程已通过。当前抖音点赞控件、状态类与回执字段已经生产登录页面及其加载的前端组件代码核对；真机执行通路已实际到达平台，点赞刷新留存未通过。已修复原成功判定遗漏刷新复核的问题。

旧 Electron 23096 与旧后台已由用户正常退出。新版完成启动、真机尝试和修正后的再次正常重启；最后主进程 11508，后台 20580，8899 正常，SCHEDULER_OFF=1。后一次使用已开启的 CDP Browser.close 正常退出，日志确认后端跟随停止，未强制结束进程。仍使用原生产登录分区，没有读取凭据。一次尝试的持久化标记已使用，真机按钮已禁用，未重新生成授权或重试。

## 已实现

- 离线目标：本地确认成功发表的主评；手填完整视频 URL、作者昵称及完整正文。服务端再次解析已有评论，不信任前端修改目标。
- 当前视频主评只读选择：指定账号、完整 URL、上限 1–60 条，去重并排除回复/其他视频。公共接口只返回不透明记录键，内部平台定位信息不外露。生产页面已实际读取，已有评论源可从精确匹配的真实只读缓存解析平台编号；同作者同正文对应多个编号则拒绝执行。
- 离线多账号依次模拟、可中断间隔等待、停止、运行中编辑/删除保护。按账号、视频、作者和规范化完整正文持久化去重。
- 新增 `start-comment-like-live`，仅接受一个账号、一条从只读缓存选择的主评、本地限时限次授权。精确绑定账号昵称、任务、视频、评论 ID、作者和正文；无授权时界面禁用。模拟接口拒绝真实模式，通用启动入口拒绝误入评论点赞。
- 桌面执行器核对账号、唯一评论和唯一点赞控件；已有点赞跳过，不取消。点击前后核对停止、授权、视图身份；持久化单次尝试，不确定结果停止并阻止自动重试。
- 真实成功要求稳定已赞页面状态、同一评论的成功平台回执、无缓存刷新后仍稳定已赞同时成立。旧的仅回执加即时状态证明不再被服务端计为成功。刷新状态消失记为不确定并停止，绝不补点；回执钩子只保留动作绑定和状态码，不保留凭据或响应正文。
- 真实 `success/stats/runs` 与 `simulated_success/simulationStats/simulationRuns` 分开；去重及预留空间分开。模拟不增加真实统计，前端保存不能覆盖真实历史。
- 读取、评论点赞、获客和养号共享账号占用保护，避免同账号页面被并发操作。
- 结果支持分页、批次、账号和状态筛选；已赞、未找到、失败、达到上限、不确定各有原因。刷新后保留历史；重启后无人拥有的运行记为中断。删除配置保留历史和去重依据。
- 日志事件和数据库回放共用同一时间戳，修复日志抽屉的重复行。
- 界面沿用原设计，增加默认禁用的受限真机入口、模式明确的结果和历史。

## 验证记录

相关 Python 回归 291 项通过，10 条既有框架警告，覆盖 comment likes、runtime、IPC、leadgen pools、persistent gate、task logs、offline tasks/simulation、aggregate views、live scope 和 task runner。

日志修正增加一项测试后，最终复跑：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_comment_likes.py backend/tests/test_live_acceptance_scope.py backend/tests/test_task_logs.py -q --disable-warnings
```

82 项通过；随后勾选问题补充一项回归，本模块现有 59 项。没有将重复执行的测试数累加成新增覆盖。

JavaScript 相关回归 57 项通过，覆盖评论定位/回执/单次尝试、scope、执行路由、停止、预览、养号门禁和 write commands。改动的前端及执行器通过 JavaScript 语法解析。

北京时间 2026-11-15 13:51，独立 Electron 桌面实际流程通过：表单创建两条目标 → 保存并模拟 → 第一条成功 → 停止 → 详情 → 日志 → 再次运行去重 → 完成 → 刷新后仍有历史。未授权真机按钮禁用，平台写操作为 0，Electron 退出码 0，日志未重复。

复现入口：

```powershell
$commentUiProcess = Start-Process -FilePath '.\desktop\node_modules\electron\dist\electron.exe' -ArgumentList @('desktop/_verify/comment_like_ui.js','--mode=steps') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -Wait -PassThru
```

证据：`desktop/_verify/comment-like-ui/result.json`、`form.png`、`results.png`、`history.png`。这是独立桌面离线证据，不能当作生产账号平台动作证据。

## 限制和下一步

- 真机控件已核对；当前平台 DOM 映射有版本约束，平台改版或状态不明时继续拒绝点击。刷新留存失败的具体平台原因未知，不推断为验证码、风控或自赞限制。
- 正式重开保持 `SCHEDULER_OFF=1`。当前限时 scope 只含本次账号、任务和评论的 like-comment 一次动作，标记已消耗。不要新建 scope 对同一条自动重试；下一次真机验收需要明确新的允许测试目标与次数。
- 读取仅当前页最多 60 条主评；没有楼中楼、完整遍历或流式读取。仅接受 `https://www.douyin.com/video/视频编号`，不承诺短链或分享文案解析。
- 已有评论源仅 `live_action_results` 中 `post-comment/sent_ok` 主评，不补造回复或首作评论。
- 模拟去重依据不是经过真机核验的平台评论身份；同视频、同作者、同正文合并。

## 14:00 已有评论无法勾选修正

用户截图中的“全选已加载评论”和单行复选框点击后始终“已选 0”。在独立 Electron 中用鼠标事件成功复现：后端候选项为 `local:数据库序号`，共享选择器的规范化函数只接受 `类型:64位十六进制摘要`，并要求前缀为已勾选的评论类型。因此每次点击更新选择时都会被清空。

修正候选项使用 `main:SHA256`，摘要绑定账号、原记录、视频、正文和时间。刷新和分页保持稳定，换账号或原内容改变不会误用旧选择。服务端继续解析旧 `local:序号` 显式保存配置。随后前端补充严格正整数 local 兼容，使尚未重启的旧后台数据也能选中；未知类型、畸形键、重复及每账号上限仍校验。用户已确认“已选 1，可以了”。本段勾选修复未修改平台评论。

相关后端回归 `test_comment_likes.py` + `test_ipc_bridge.py` 共 165 项通过。新增 `desktop/_verify/comment_like_picker_ui_steps.js`，`comment_like_ui.js --mode=picker` 的实际 Electron 鼠标勾选验收通过：单选、取消、全选、取消全选、已选列表、保存、重新打开回显、离线运行。证据 `desktop/_verify/comment-like-picker-ui/result.json`、`selected.png`、`saved.png`。平台写操作 0。

勾选修正当时仅有独立桌面证据；现在正式常驻实例已正常重启加载新版。新旧键格式均有实际 Electron 鼠标勾选证据：`comment-like-picker-ui/result.json` 和 `comment-like-picker-legacy-ui/result.json`；键校验测试另有 4 项。

## 14:57 后真机尝试与刷新复核

- 任务 `comment_like_86dff25b-055c-4d66-8247-1c13b06cc578`，账号“噜噜噜”，当前只有一个执行账号与一条已有主评。视频编号 `7659638318140788003`，评论编号 `7708219323638399787`。本地正文经 NFKC 后与平台读取正文精确一致，无须扩大匹配范围。
- 当前页面使用 `.comment-item-stats-container p.VpA2NKl1`，加载的组件明确以 `tOgCrAK_` 表示已赞。保留账号、作者、完整正文、评论编号、唯一控件、遮挡与停止检查；不是看到心形就点。
- 北京时间约 14:57:56 发生唯一一次点击，绑定该评论的 `/aweme/v1/web/comment/digg/` 回执 HTTP 200、status_code 0、digg_type 1。即时页面显示已赞/1；这是提交时证据，不能证明刷新留存。
- 约 15:00 及 15:04 两次独立只读刷新，直接观察页面实际 `/aweme/v1/web/comment/list/` 响应，均返回目标评论 user_digged=0、digg_count=0，没有再发点赞。没有登录墙或验证码。平台未给出未留存原因。
- 原运行 `7ab70186d8294a10927c948016eeb325` 已由严格本地审核脚本降为 uncertain。保留 originalAcceptance 与 refreshReview；同步修正结果、运行、任务统计与真实动作记录，真实新增为 0、待核验为 1；两层持久化去重均保留 uncertain，不释放重试。任务日志追加刷新复核结论，旧即时日志保留为过程记录。
- 桌面适配器新增真正无缓存刷新及再次账号/唯一评论已赞检查；刷新能力缺失则不点。停止、刷新失败、刷新变回未赞都为 uncertain。服务端只接受新版 `comment_like_receipt_state_and_reload_verified` 证明。新判定已通过离线回归并加载到重启后的正式实例，但本次一次额度已用完，没有再次真机提交。
- 界面已显示真实新增 0、待核验 1，使用运行时间；已有评论确认框会先解析实际选中目标，列出视频、作者和全文。scope 已消耗时入口禁用，提示禁止重复提交。
- 回归：相关 Python 217 项通过，10 条既有框架警告；审核日志补充后本模块 64 项复跑通过。JavaScript 17 项通过，覆盖真实 DOM 映射、刷新丢赞、刷新失败/停止、一次点击及已用 scope；改动前端与执行器语法检查通过。离线结果不代替真机成功。

证据：`data/acceptance/comment-like-live-20261115.json` 是原即时回执/状态快照，`comment-like-refreshed-20261115.json` 是刷新后的真实列表响应摘要。应一起阅读，以后者及数据库审核状态为最终结论。`desktop/_verify/comment-like-live-after.png` 是即时已赞截图，`comment-like-live-reviewed.png` 是修正后的最终界面。证据不含密码、Cookie、Token 或签名密钥。

本轮最终结论：真实提交通路已执行一次；平台留存未通过，结果不确定。误计成功的问题已修复，正式桌面已加载修正。不能对用户宣称“真实点赞已成功”或“完整真机验收通过”。

## 15:23 新评论单次真机复测

用户明确同意更换一条评论继续验证，本轮仅新增一个目标、一次真实点击，未重试旧目标，也未开放批量写操作。

- 正式桌面的“指定评论 → 读取评论并选择”读取同视频 5 条主评，选中 willma 的“不错,和直接使用cc或者codex有什么优劣?”。真实读取缓存 sampleKey 为 `0fe3d77290f67aeff29f5dbc3335773f`，平台评论编号 `7668718422674555663`，执行账号仍为“噜噜噜”。点击前唯一评论匹配且未赞。
- 创建并保存任务 `comment_like_22e82508-44c4-4e2b-b43e-65e7103054e4`，每账号上限 1；通过正常 `Browser.close` 退出旧桌面及所属后端，重启加载该目标的单次 scope，`SCHEDULER_OFF=1`。当前 Electron PID 23396、后端 PID 4612，端口 9222/8899。本轮未强杀进程。
- 北京时间 15:22:51 从桌面确认框启动；约 15:22:56 的真实 comment/digg 回执 HTTP 200、status_code 0、digg_type 1，绑定准确评论和视频。被动 CDP 观察器仅记录这些脱敏字段，不重放请求。
- 适配器自动无缓存刷新后，平台真实 comment/list 返回 user_digged=0、digg_count=1；该评论点击前即有 1 个赞，不能把总赞数当成本账号成功。15:23:09 独立只读刷新再次返回同样状态。当前账号昵称仍为“噜噜噜”，页面未检测到可见验证码提示；具体未留存原因未知。
- 新版判定直接生成 uncertain，无须事后降级。运行 `8c961890b4a34adb805ef1c8f3504f4f` 已结束：处理 1、真实新增 0、待核验 1，原因“平台返回成功但刷新后未保留点赞，已停止且禁止自动重试”。scope 已消耗、真机入口禁用，后台无活跃点赞任务。与旧任务合计两次真实尝试，两次均未证明留存成功。
- 正式桌面详情已目视核对上述真实统计与原因，留在此页面供用户查看。平台 BrowserView 截图超时，没有将旧截图冒充本轮平台证据；本轮使用实际网络摘要和新拍摄的任务详情图。

本轮工具补齐：`prepare_comment_like_scope.py` 支持从真实缓存精确解析单条指定主评，拒绝正文篡改、缺失缓存、多目标、非主评、无效编号与身份歧义，保留本地已有评论路径。scope 输出仍排除密钥、限 30 分钟、单动作且禁止覆盖旧文件。只读复核与证据工具支持独立输出文件并禁止覆盖，保留首轮证据。

回归：`test_comment_likes.py` 71 项通过、10 条既有框架警告，其中授权准备 8 项；新观察器和两个修改后的证据脚本均通过 Node 语法检查。没有将离线测试记为真实成功。

本轮证据：

- `data/acceptance/comment-like-willma-network-20261115.json`：唯一真实点赞回执及动作前后平台列表字段。
- `data/acceptance/comment-like-willma-refreshed-20261115.json`：独立无缓存刷新、目标 user_digged=0。
- `data/acceptance/comment-like-willma-live-20261115.json`：最终未赞 DOM 探针及数据库真实 uncertain 结果。
- `desktop/_verify/comment-like-willma-reviewed.png`：本轮任务详情截图；`comment-like-willma.out.log` / `.err.log`：桌面启动日志。

结论：真实点击与提交回执已验证，刷新留存仍未通过。程序已正确停止、未误记成功。后续如需区分账号/平台/桌面环境因素，应先由用户在平台正常界面对照检查，不自动追加点赞或绕过平台验证。

## 15:40 页面默认改为真机模拟（当前版本）

用户明确要求正常页面使用真机模拟而非离线模拟。本段是当前操作入口说明，取代前文“默认离线、需开发者生成 scope 后重启才能测试”的旧流程；前述真机未留存的历史结论保持不变。

- 正常页面按钮为“真机模拟”，新建弹窗为“保存并真机模拟”。默认显示真实运行状态、真实新增与真实详情，不调用离线模拟接口。原离线任务在真实视图显示“待真机执行”，真实计数从 0 开始；原模拟记录仍保留在“离线测试记录”入口。
- `get-comment-like-runtime` 提供每个任务、每种模式各自最新的运行快照。前端按当前视图选择状态、时间和计数，避免最后一次离线测试覆盖真实运行状态，或把模拟完成改名成真机完成。详情摘要同样分开。
- 新版桌面内点击后，由 Electron 主进程从本地已保存任务和真实读取缓存解析目标，检查此前已发送/待核验的持久化记录。只接受一个执行账号、一条准确主评；展示原生确认框，列出账号昵称、视频、作者和全文，用户取消不会创建授权或启动任务。
- 确认后再次读取并比对配置指纹，创建新的单次准确授权，再通过原有 `start-comment-like-live` → 签名命令 → 桌面原生点击 → 回执与刷新留存验证执行。授权不依赖上一轮已耗尽的开发测试文件，也不需要用户每次重启。没有打开批量动作、作品发布、私信或其他写操作。
- 只有本机主窗口的 IPC 可申请此原生确认；HTTP 请求本身不能生成授权。`LEADS_COMMENT_LIKE_SCOPE` 由桌面启动时注入后端及 worker，单独用于评论点赞，其他动作仍使用原有验收范围。原生进程串行核对，禁止并发替换授权；持久化 uncertain/sent_ok 不会因新建任务而释放。
- 每次仍为一个账号、一条评论、一次尝试；刷新未保留仍计 uncertain，不会因为页面改为真机而宣称平台留存通过。本轮修复没有新增平台点赞。

验证：后端相关 192 项回归通过，随后新增的原生 scope 隔离测试所在文件 14 项通过；Node 评论适配器、授权、启动器共 20 项通过（19 项初跑后新增 1 项路由隔离用例，启动器文件 7 项复跑通过）。新版实际 Electron 隔离桌面验收通过：保存及列表按钮均调用 native real 通道；模拟取消无任务启动；离线测试需独立入口，测试成功后回到真机视图仍为待执行、真实 0；详情无模拟成功卡片。测试环境无生产登录态、平台写操作为 0。

额外旧桌面 steps 回归的启动命令被自动审批拦截、未给出具体原因，没有继续执行该命令；本轮采用已通过的新版专用隔离桌面验收及单元回归作为验证依据。

正式客户端已正常退出后重启加载新版，Electron PID 17856、后端 PID 11340，`SCHEDULER_OFF=1`。目视检查正常页按钮/状态/计数；调用正式原生入口核对旧 uncertain 目标，返回“该账号对此评论已执行或结果待核验，禁止重复提交；请选择另一条评论”，未启动动作。当前界面保留在正常真机操作列表，供用户自行验证新目标。

证据：`desktop/_verify/comment-like-real-default-ui/result.json`、`real-default.png`、`real-details.png` 为隔离桌面证据；`desktop/_verify/comment-like-real-default-production.png` 为正式桌面新版页面截图。启动日志 `comment-like-real-default.out.log` / `.err.log`。没有新增真机成功证据。

新增主要实现为 `desktop/comment-like-launcher.js`、`ui/assets/CommentLikeMode.js`；其余修改在 `desktop/main.js` / `preload.js`、`ui/live-bridge.js`、`ui/assets/CommentLikeTasks-C67c1eAA.js`、`backend/tools/prepare_comment_like_scope.py`、`backend/app/comment_likes.py` 和 `backend/app/execution/live_scope.py`。新的确认能力已加入桌面打包文件列表。

## 主要文件

2026-11-15后续更新：评论点赞已接入全局实况，原生确认后自动打开本账号画面；增加本次运行首帧门禁、准确停止与全部停止、短任务结束留痕及刷新恢复。详见 `handoff-live-dashboard-integration-2026-11-15.md` 与 `global-live-dashboard-prd.md` 第12节。此轮213项Python、26项Node和两组隔离Electron验收通过；正式桌面已加载，未新增真实点赞。正式截图工具超时，未宣称本轮真实动作验收通过。

- `backend/app/comment_likes.py`、`backend/app/api/ipc_bridge.py`：任务、专用接口、运行和持久化。
- `backend/app/execution/live_scope.py`、`live_leadgen.py`：精确范围及签名传输。
- `backend/app/runtime/registry.py`、`browser.py`、`backend/app/api/task_runner.py`：账号占用及执行路由。
- `desktop/comment-like.js`、`live-platform.js`、`action-executor.js`、`douyin_agent.js`：定位、回执、受限单次点击、停止和坐标保护。
- `ui/assets/CommentLikeTasks-C67c1eAA.js`：原界面接线、模式、结果和历史。
- `backend/tests/test_comment_likes.py`、`desktop/_verify/test_comment_like.js`：回归测试。
- `backend/tools/comment_like_ui_fixture.py`、`desktop/_verify/comment_like_ui.js`、`comment_like_ui_steps.js`：独立桌面验收。
