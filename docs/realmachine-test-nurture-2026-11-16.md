# 养号模块真机测试报告（2026-11-16）

## 1. 测试概况

| 项 | 内容 |
|---|---|
| 测试对象 | 阿里山获客桌面壳 → 养号（self-warmup）执行链路、评论获客只读预览 |
| 测试环境 | Windows 本机，系统缩放 165%，采集窗口 1280×900（页面视口 1280×900，zoomFactor=1） |
| 被试账号 | `3732e4b2a76f4d059ee9fea7e7781e9b`（AI学长小马 / 噜噜噜） |
| 交互对象 | 抖音 Web 端真实平台、真实登录态 |
| 测试时间 | 2026-11-16 21:32 – 22:21（共 10 轮真机运行） |
| 引擎 | `shell`（桌面壳内采集窗口） |
| 判定基线 | 点赞成功 = 平台回执 `status_code=0`；浏览成功 = 确认同一视频持续播放 |

**方法**：每轮用 `start-task` 起一次 2 分钟养号（点赞概率 100%、每屏停留 5–7 秒、单屏滚动 1 次），
同时盯三处证据：壳日志 `desktop/_verify/agent.log` 的 trace、库表 `runtime_runs / runtime_cycles /
task_runtime_logs`、以及点赞事件增量（`account_events where action='like'`）。

---

## 2. 结论摘要

| 编号 | 用例 | 结果 | 关键证据 |
|---|---|---|---|
| A1 | 浏览计数与屏数一致 | ✅ 通过 | 9 轮 → 浏览 9 屏；`runtime_cycles.watched` 合计一致 |
| A2 | 信息流点赞（真实回执） | ✅ 通过（本轮修复后） | 连续 9 屏 9 赞，全部 `点赞接口回执 status_code=0` |
| A3 | 中途手动停止 | ✅ 通过 | `status=stopped`，message「手动停止，未跑满设定时长」 |
| A4 | 养号冷却守卫（3 分钟） | ✅ 通过 | `retryAfterSec:118`「距上次养号结束不足 3 分钟」 |
| A5 | 页面参数与已存配置一致性守卫 | ✅ 通过 | 传 duration 5 / like 0 → 「养号页面参数与已保存配置不一致」 |
| A6 | 静帧/自动播放被拒的容错 | ✅ 通过（本轮修复后） | 单屏跳过不计数；连续 3 屏才判本轮失败 |
| A7 | 平台人机验证拦截 | ✅ 通过 | 「抖音要求安全验证｜采集在搜索页被拦下…未做任何点击」 |
| A8 | 评论获客只读预览零写入 | ✅ 通过 | `live_action_results` 82→82、`write_reservations` 16→16 |
| A9 | 僵尸运行收尾 | ✅ 通过 | 壳被杀 → 运行落 `failed` 并写明原因；启动时 `reap_stale_runs` 收尾 |
| B1 | 评论获客预览成功分支（有结果） | ⚠️ 未覆盖 | 账号当前处于验证态，搜索页被拦，见 §6 |

---

## 3. 本轮定位并修复的 4 个缺陷

### 缺陷 1：点赞路径被写动作门禁整条拦死（0 赞的直接原因）

- **现象**：点赞概率 100%，每轮日志却是「未尝试 1」，`account_events` 的 like 零增量。
- **根因**：`desktop/action-executor.js` 的 `rejectLiveAction` 对 `like-video` 无条件返回拦截，
  而养号点赞正是这条路径 → 变成死代码；且拦截被统计成「未尝试」，用户看不出是「被拦」而不是「没中」。
- **修复**：养号点赞的写确认来自任务本身（用户设概率、点开始，目标就是当前在播视频），
  改为只拦「连执行账号都没有」的调用；被拦时单独记 `write-blocked`，与「未尝试」分开。
- **证据**：修复后日志从「未尝试 1」变为「无效 1」（真点了没中），再修复下文两项后变为「点赞 1 次」。

### 缺陷 2：可信鼠标事件根本没进页面（隐藏窗口）

- **现象**：探针找到的按钮完全正确（`data-e2e-state="video-player-no-digged"`、命中测试通过、
  `rect=1149,424,45,68` 与点击坐标一致），但平台没有任何回执。
- **证据链**：
  - 隐藏窗口：`事件 win=0/0/0 doc=0/0/0 vis=hidden`
  - 移到屏幕外 `(-4000,-4000)` 再 `showInactive`：`事件 win=1/1/1 doc=1/1/1 vis=hidden`
    —— 窗口已 show、输入能进，但整块不可见区域仍被判 hidden
  - 屏幕内 + `setOpacity(0)`：`vis=visible`，点赞开始出现真实回执
- **修复**：`worker-surface.js` 的 `showQuiet` 改为「摆回屏幕内 + 透明度 0 + 不抢焦点」，
  `hideQuiet` 收回时恢复位置与透明度；`likeOnCurrentPage` 在点击前 `showQuiet`、点完 `hideQuiet`。
- **说明**：本地对照实验（`_verify/input_probe*.js`）证明隐藏窗口本身能接收 `sendInputEvent`，
  所以问题不在「窗口隐藏」这个动作，而在页面 `visibilityState` 影响抖音自身的处理器。

### 缺陷 3：页面隐藏后视频被暂停，点赞处理器直接短路

- **现象**：修好可见性后第一轮 8 屏只有前 2 屏点赞成功，其余全部 `click-no-effect`。
- **证据**：`事件 win=1/1/1 doc=1/1/1 vis=visible 播放器=暂停` —— 事件进页了、按钮也命中，
  但播放器处于暂停态时抖音不产生 `/digg` 请求。
- **根因**：每轮结束 `hideQuiet` 隐藏窗口 → 页面暂停；下次重新可见后浏览器不会自动续播。
- **修复**：`likeOnCurrentPageInner` 点击前先确认播放：`video.paused` 则 `play()`，
  仍未播起来再静音重试一次（采集页不可见，静音无感）。
- **效果**：连续 9 屏 9 赞，成功率 100%。

### 缺陷 4：单屏确认不了播放就让整轮失败

- **现象**：21:42 与 21:46 两轮都在「第 4 轮」整轮失败——
  「没有确认当前视频持续播放，未增加浏览数」。
- **根因**：后台窗口偶发自动播放被拒、视频刚好播完，都会触发这条硬失败。
- **修复**：单屏跳过、多等一拍再切下一条，**连续 3 屏**确认不了才判本轮失败；期间一屏都不计入浏览数（不伪造）。
- **回归**：`_verify/test_warmup_offline.js` 由「单屏静帧即报错」改为「单屏跳过 + 三屏失败」两条断言。

### 附带修正：日志措辞

「其中 1 个在视频详情页完成」改为按实际路径输出：信息流点赞写「在信息流完成」、详情页点赞写「在视频详情页完成」。
（此前后端进程是 20:32 启动的、未带 `--reload`，改动没生效，日志一直是旧文案；重启后端后已正确。）

---

## 4. 真机实测数据（10 轮）

| 时间 | run | 结果 | 屏 | 赞 | 说明 |
|---|---|---|---|---|---|
| 21:32 | war_7ec9e0537297 | completed | 13 | 0 | 门禁拦死，全程「未尝试」 |
| 21:37 | war_d90157e20c49 | completed | 7 | 0 | 同上，另暴露按屏探测问题 |
| 21:42 | war_e62db0c14869 | failed | 2 | 0 | 第 4 屏静帧硬失败 |
| 21:46 | war_23cddf7fd08a | failed | 3 | 0 | 第 4 屏静帧硬失败 |
| 21:51 | war_b54ade0c2726 | failed | 4 | 0 | 我杀掉壳导致的超时（用例 A9 的证据） |
| 21:59 | war_2eee80518535 | completed | 5 | 0 | 屏幕外方案：事件 1/1/1 但 vis=hidden |
| 22:05 | war_d0cbb3fab338 | completed | 8 | 3 | 透明度 0 方案：首次真实点赞成功 |
| 22:10 | war_395223355dd0 | completed | 9 | 9 | 续播修复：连续 9 屏 9 赞 |
| 22:15 | war_22a0593a1d9b | completed | 9 | 9 | 新后端：措辞「在信息流完成」正确 |
| 22:20 | war_24bbe6a6cfff | stopped | 1 | 1 | 手动停止用例 |

`account_events` 的 like 总数：测试前 **19** → 测试后 **41**，净增 22（3+9+9+1），与运行统计逐轮对齐。
点赞成功全部带平台回执 `status_code=0`，不是 DOM 变化推断。

---

## 5. 关键日志原文

```
# 修复前：事件根本没进页面
13:51:23.589 信息流点赞: https://www.douyin.com/?recommend=1 → click-no-effect
  点击后计数/样式未变化｜命中按钮｜事件win=0/0/0doc=0/0/0vis=hidden｜rect=1149,424,45,68

# 屏幕外方案：事件进了、页面仍 hidden
14:00:02.426 → click-no-effect｜事件win=1/1/1doc=1/1/1vis=hiddendc:path

# 透明度 0 后首次成功
14:05:43.352 信息流点赞: https://www.douyin.com/?recommend=1 → liked 点赞接口回执 status_code=0

# 暴露暂停短路
14:05:55.883 → click-no-effect｜事件win=1/1/1doc=1/1/1vis=visible播放器=暂停

# 续播修复后：连续成功
14:11:04.847 → liked 点赞接口回执 status_code=0
14:11:14.430 → liked 点赞接口回执 status_code=0
14:11:23.791 → liked 点赞接口回执 status_code=0

# 后端日志（新代码）
第 1 轮：浏览 1 屏，点赞 1 次，跳过 0｜已赞 0｜无效 0｜未尝试 0｜无控件 0，其中 1 个在信息流完成

# 人机验证拦截（未做任何点击）
[通知] 抖音要求安全验证 | 采集在「搜索页」被拦下（需要人工过验证）。采集窗口已在后台保持，未做任何点击。
```

---

## 6. 未覆盖 / 遗留

1. **评论获客预览成功分支（B1）**：本轮预览被抖音人机验证拦在搜索页，
   返回「平台需要人工验证，请在实况大屏完成验证后重新准备；尚未发送」——行为正确但未能拿到筛选结果文案。
   需要在托盘菜单「显示采集窗口」手动过一次验证后重跑。
2. **收藏 / 分享 / 关注 / 附图 / GIF / @ 账号**：需单独授权，本轮未做（写动作影响面大，按最小必要原则跳过）。
3. **页面元素在长时间运行下的选择器稳定性**：本轮 10 轮均命中 `data-e2e-state="video-player-no-digged"`，
   但抖音改版风险仍在，`LIKE_PROBE_JS` 的多候选 + 命中测试 + `el` 片段回传已作为兜底诊断保留。

---

## 7. 数据影响与清理

- **真实点赞 22 次**（真实账号、真实视频），由本轮测试产生，未撤销。
  如需回收请人工在抖音端取消，程序不做「点赞后取消」这类高风险动作。
- **测试用的养号配置**已写入 `system_settings(category='nurture')`：
  `duration 2-2、like 100%、collect/share 0、stay 5-7`，原配置备份在
  `_test-artifacts/nurture-config-backup.json`（测试完已恢复为原值）。
- 运行记录（`runtime_runs / runtime_cycles / task_runtime_logs`）保留作为证据，未删除。

---

## 8. 复现与回归命令

```bash
# 桌面壳（改了 douyin_agent.js / worker-surface.js 必须重启壳才会生效）
cd desktop && env -u ELECTRON_RUN_AS_NODE ./node_modules/.bin/electron .

# 起一轮 2 分钟养号（payload 必须包在 args 里）
curl -s -X POST http://127.0.0.1:8899/ipc/start-task -H "Content-Type: application/json" \
  -d '{"args":[{"taskId":"nurture_<uid>_<ts>","taskMode":"nurture","taskKind":"nurture",
      "accountId":"<uid>","platform":"douyin","name":"…","nickname":"…",
      "nurtureDurationMin":2,"nurtureDurationMax":2,"nurtureLikePercent":100,
      "nurtureCollectPercent":0,"nurtureSharePercent":0,"stayMin":5,"stayMax":7,
      "scrolls":1,"viewKey":"douyin_<uid>"}]}'

# 看点赞 trace
grep -aE "信息流点赞|点赞点击" desktop/_verify/agent.log | tail -5

# 查库（必须 isolation_level=None，否则读事务看不到新行）
#   runtime_runs / runtime_cycles / task_runtime_logs / account_events(action='like')

# 离线回归
cd desktop && env -u ELECTRON_RUN_AS_NODE node --test _verify/test_*.js      # 145 passed
cd backend && .venv/Scripts/python.exe -m pytest -q                          # 818 passed
cd backend && .venv/Scripts/python.exe -m pytest tests/test_runtime.py tests/test_nurture_w3.py -q   # 61 passed
```

---

## 9. 风险与建议

1. **`setOpacity(0)` 的依赖**：依赖 Windows 分层窗口；若某机器上不可用，点赞会退回「事件进页但无回执」。
   建议在 `showQuiet` 返回值里带上实际生效手段，并在连续多轮 0 赞时提示用户。
2. **点赞频率**：本轮为验证目的跑满 100% 概率（2 分钟 9 赞）。真实运营建议保持默认 10% 左右，
   短时间高频点赞更容易触发人机验证（本轮搜索页已被验证拦截，可作为旁证）。
3. **后端与壳的加载关系**：后端由桌面壳用 `backend/.venv` 自动拉起，改后端代码必须重启壳（或重启后端进程），
   否则日志/统计会继续用旧代码（本轮就踩了一次，导致措辞一直是旧的）。
4. **静帧容错阈值**：3 屏是经验值；若后续出现「视频刚好播完」频繁误判，可调成按占比判断。
