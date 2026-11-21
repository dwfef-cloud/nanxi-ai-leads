# 消息聚合提醒弹窗被聊天页遮挡（2026-11-15）

用户截图显示“后台新消息提醒”弹窗已打开，但抖音聊天原生视图仍覆盖在其上方，遮挡设置项和按钮。本轮只处理该页面问题，没有发送消息、评论或修改后台提醒配置。

## 原因

- `ui/assets/MessageCenter-6X3AljGg.js` 已在后台提醒、声音提醒及全局弹窗开启时发送 `set-chat-view-attached`，但载荷仅为 `{attached:false}`，关闭时同样只发送 `{attached:true}`。页面卸载发送的 `destroy-chat-view` 也没有视图编号。
- 原 `desktop/main.js` 对缺少 `viewKey` / `accountId` 的指令使用 `chat_default`；实际打开的视图键为 `douyin_<账号id>`。因此隐藏指令找不到当前视图，返回成功但没有收起聊天页面。单纯提高网页弹窗的 CSS 层级不能处理原生视图。
- 原临时隐藏分支清空当前视图键，无法支持无编号的恢复；布局同步在等待宿主坐标时也可能收到晚到结果，在弹窗打开或切换账号后重新挂回旧视图。

## 修改

- `ui/live-bridge.js` 记住消息和创作页各自选中的原生视图，为无编号的隐藏、恢复、刷新、卸载指令补齐编号；兼容当前仍运行旧主进程的客户端。保留显式目标，不修改调用方对象，卸载后清理前端选中记录。
- `ui/index.html` 更新桥接脚本版本参数为 `20261115-chat-modal`，主界面刷新后加载新脚本。
- `desktop/main.js` 在操作现有视图时回退到当前选中编号；临时收起保留选择，销毁清理选择；刷新/销毁不存在的页面不再额外创建默认视图。布局请求返回后重新检查可见性和选中账号，避免晚到布局把聊天页挂回弹窗上方。主进程加固在下一次正常启动后加载，前端兼容修复只需刷新。
- 本轮未改消息页布局、账号数据、实况大屏布局和平台写操作授权。

## 验证

- `node --test desktop/_verify/test_aggregate_modal.js desktop/_verify/test_leadgen_launcher.js desktop/_verify/test_dashboard_layout.js`：14 项通过，其中新增 7 项覆盖真实主进程函数及正式前端桥接脚本的消息隐藏/恢复、卸载、延迟布局、账号切换、显式目标和创作页隔离。
- 新增隔离 Electron 验收 `desktop/_verify/aggregate_modal_native.js`，仅使用本地 HTML 和独立测试配置目录，真实 BrowserWindow / BrowserView 挂载：提醒弹窗开启后原生视图数为 0，关闭后恢复同一 webContents，页面卸载后原生视图数为 0；主页面上的弹窗按钮位于命中测试顶层。没有使用生产账号或平台网络。
- 原生测试结果和图片：`desktop/_verify/message-modal-20261115/result.json`、`notification-modal-visible.png`。提醒弹窗截图已人工查看，内容及按钮完整显示；恢复聊天通过原生视图与 webContents 身份断言核对，没有宣称取得恢复后聊天的原生层截图。
- 修改文件 JavaScript 语法检查通过。

## 正式客户端状态

- 已通过 Windows accessibility 读取正式窗口：后台提醒弹窗确实打开，聊天文档仍在原生层，符合截图问题；当前键盘焦点在聊天页。
- 桌面点击工具此前持续报 `coordinate input geometry is unavailable`，不能可靠点击主界面取得焦点。已请求用户点白色弹窗标题后按 Ctrl+Shift+R，刷新后重新打开后台提醒核对。尚未收到正式客户端刷新后的结果，不能将隔离桌面通过写成正式界面已通过。
