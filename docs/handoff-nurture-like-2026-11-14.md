# 交接：养号功能「点赞点不中」问题（2026-11-14）

> 2026-11-15 更新：最新结果见 [桌面真机验收交接](handoff-live-acceptance-2026-11-15.md)。下文保留 11 月 14 日交接时点事实，不代表当前状态。逐项验收成功不等于养号自动互动已全部接通。

> 交接对象：codex
> 一句话：养号能真实浏览、能进视频详情页，但**点赞点下去平台没收到请求**，根因已定位到「CSS 像素 vs 窗口 DIP 坐标换算」，修复代码已写完，**尚未做真机复验**。

---

## 1. 目标与验收标准

养号任务（`taskMode: "nurture"`）在真机上应该做到：

1. 打开抖音推荐流，按配置滚动/停留（已达成）
2. 按 `nurtureLikePercent` 概率点赞，**点赞必须是平台真实认账的**
3. 日志/页面上的点赞数只能是真实成功的数量，不允许估算或伪造

**验收判据（关键）**：`window.__diggLog` 里出现点赞接口回执且 `status_code=0`（或点赞按钮 DOM 明确变化）。只看"点击动作已发出"不算成功。

---

## 2. 当前状态

| 环节 | 状态 |
| --- | --- |
| 启动/停止任务、进度事件、轮次日志 | ✅ 已通（上一轮工作完成） |
| 真实浏览（滚动 + 停留 + 视频数统计） | ✅ 已通，4 轮 16 屏实测 |
| 从精选页点开视频详情层 | ✅ 已通（改真实鼠标事件后） |
| 详情页找到点赞按钮 | ✅ 已通（命中测试通过） |
| **点赞真正生效** | ❌ **未通，本轮交接重点** |
| 收藏 / 转发 | ❌ 执行器未实现（配置了也不执行，日志会明说） |
| 评论获客 `post-comment` | ❌ 执行器未接 |

最近三次真机结果（全部 `liked: 0`）：

```
war_582b6f04016e  7 轮 7 屏  点赞 0  136.3s
war_b3c2cabee62a  7 轮 7 屏  点赞 0  135.1s
war_fdc0838ca3e3  4 轮 16 屏 点赞 0  228.6s
```

---

## 3. 根因链（四个坑，前三个已修，第四个是核心）

### 坑 1：脚本合成 click 被抖音忽略（已修）

**现象**：点卡片后 `clicked: true, likeReady: false, url` 不变、也没有新窗口请求。

**原因**：精选页卡片是 `div + img`（整页无 `/video/` 锚点），点击处理挂在 React 上，`dispatchEvent(new MouseEvent(...))` 是 `isTrusted=false`，直接被忽略。

**修法**：拆成「只读探坐标」+「Node 侧 `webContents.sendInputEvent` 真实点击」。

### 坑 2：注入脚本被模板字符串吃转义（已修，需长期防）

`/\/video\/\d{6,}/` 这类正则写进模板字符串后，`\/`、`\d` 会在求值阶段被吃掉一层，注入到页面直接 `Unexpected token ')'`。

**规矩**：注入脚本内**禁止写正则字面量**，一律用 `indexOf` / `[^0-9]` 这类安全写法。

**防线**：`desktop/_verify/check_warmup_js.js` 会按运行时求值后再做语法校验，**改完注入脚本必须跑一次**：

```bash
cd desktop && node _verify/check_warmup_js.js
```

### 坑 3：点赞按钮取到了视口外那个（已修）

**现象**：`详情层已开: rect=2011,672,74,112 候选=1 命中=按钮`，点击后无任何反应。

**原因**：页面里挂了多个 `[data-e2e="video-player-digg"]`（详情浮层 + 底层预加载卡片），`querySelector` 取第一个可能不在可见区。

**修法**：`LIKE_PROBE_JS` 改成枚举全部候选 + `document.elementFromPoint` 命中测试，只保留"点得到"的那个。

### 坑 4：CSS 像素 ≠ sendInputEvent 的 DIP 坐标（**核心，刚修完未验**）

**真机硬数据**：

```
详情层已开: rect=2011,672,74,112 候选=1 vp=2115x1397 命中=按钮
```

- 采集窗口：`BrowserWindow({width: 1280, height: 900})`
- 页面 `window.innerWidth × innerHeight` = **2115 × 1397**
- 比值 `1280 / 2115 ≈ 0.605`（系统显示缩放 165%）

`getBoundingClientRect` 返回的是 **CSS px**（2048, 728），而 `webContents.sendInputEvent` 收的是 **DIP**（窗口坐标系）。原代码把 CSS 坐标直接传进去 → 实际点击位置放大 1.65 倍 → 落在窗口外 → 浏览器根本没收到事件 → 点赞接口一次都没被调用（`window.__diggLog` 为空）。

这也解释了一个"看起来矛盾"的现象：**卡片能点开（CSS 747 → 偏到 ~1235，仍落在宽 1127 的大卡片上，侥幸命中），点赞永远点不中（按钮在最右侧，偏出更多）**。

**修法**：新增 `viewScale(view)`，用 `getContentBounds() / innerWidth` 算出比例，所有 `sendInputEvent` 坐标先乘比例。

---

## 4. 本次改动的代码清单

文件：`desktop/douyin_agent.js`（1550 行左右，函数名定位比行号可靠）

| 位置 | 内容 |
| --- | --- |
| `const LIKE_PROBE_JS` | 重写：枚举全部候选取坐标 + `elementFromPoint` 命中测试；返回 `x/y/sig/already/hitOk/hitCls/rect/count/viewport` |
| `const DIGG_HOOK_JS`（新增） | hook `fetch` / `XMLHttpRequest`，把 `/digg`、`commit/item/digg`、`like/write` 的回执记到 `window.__diggLog`（幂等，最多 20 条） |
| `const CARD_PROBE_JS`（新增，替代 `CLICK_CARD_JS`） | 只读：返回精选页第一张可点卡片的中心坐标 + 命中元素 |
| `async function viewScale(view)`（新增） | 算 CSS→DIP 比例，`{x, y, vp, box}` |
| `function judgeDigg(entries)`（新增） | 解析回执：取 `status_code` 后 26 字符、剔非数字，首位 `0` 判成功，否则带 code 判失败。**不用正则** |
| `async function likeOnCurrentPage(view)` | 加：坐标换算、点赞前 `DIGG_HOOK_JS` 注入、点击后同时用 DOM 变化 + 接口回执判定；`hitOk=false` 时先点画面收遮罩再探 |
| `async function clickCardToDetail(view)`（新增） | 真实鼠标点卡片 → 轮询等点赞控件出现（最多 8.4s），trace 出 `css/dip/scale` |
| `async function warmupWatch(cmd)` | `loadPage` 后注入 `DIGG_HOOK_JS`；无直链分支改调 `clickCardToDetail` |

校验脚本：`desktop/_verify/check_warmup_js.js` 的清单里已加入 `CARD_PROBE_JS`、`DIGG_HOOK_JS`。

后端本次**未改动**（保持上一轮已验收的状态）。

---

## 5. 复现 / 验证环境

### 启动

```bash
# 后端（若未起）
cd nanxi-ai-leads/backend && python -m uvicorn bridge_server:app --port 8899

# 桌面壳（改了 douyin_agent.js 必须重启壳，模块不会热更新）
cd nanxi-ai-leads/desktop && env -u ELECTRON_RUN_AS_NODE ./node_modules/.bin/electron .
```

### 启一个养号任务

```bash
curl -X POST http://127.0.0.1:8899/ipc/start-task -H "Content-Type: application/json" -d '{
  "args": [{
    "taskId": "nurture_<accountId>_<ts>",
    "taskMode": "nurture", "taskKind": "nurture",
    "accountId": "3732e4b2a76f4d059ee9fea7e7781e9b",
    "platform": "douyin", "name": "AI学长小马", "nickname": "噜噜噜",
    "nurtureDurationMin": 2, "nurtureDurationMax": 2,
    "nurtureLikePercent": 100, "nurtureCollectPercent": 0, "nurtureSharePercent": 0,
    "stayMin": 5, "stayMax": 7, "scrolls": 1,
    "viewKey": "douyin_3732e4b2a76f4d059ee9fea7e7781e9b"
  }]
}'
```

> ⚠️ 入参**必须**包在 `args: [...]` 里，直接平铺会被当空参（`缺少 taskId，无法启动`）。
> 验证期用 `nurtureLikePercent: 100` + `scrolls: 1`，每轮必然尝试一次点赞，2 分钟能跑 6~7 轮。

停止：`POST /ipc/stop-task`，body `{"args":[{"taskId":"..."}]}`。

### 看结果

壳侧 trace（最有用）：`desktop/_verify/agent.log`，过滤关键词

```bash
grep -aE "点卡片进详情|详情层已开|点赞点击|点卡片进详情点赞" desktop/_verify/agent.log | tail
```

库里（`data/lead_system.db`）：

- `task_runtime_logs` —— 页面看到的日志流水
- `runtime_runs` / `runtime_cycles` —— 运行与轮次（`status` 只有 `running/ok/failed/stopped/aborted`）
- `task_runtime_logs.message` 里 `无效 N` = 点了没成功的数量

> 查库时用 `sqlite3.connect(..., isolation_level=None)`，否则读事务会锁住快照，看不到新日志（排查时被坑过）。

---

## 6. 下一步（建议 codex 按此顺序）

1. **重启壳跑一轮**，看 `agent.log` 里 `点赞点击: css=..,.. dip=..,.. scale=0.605`。
   - `scale` 应该 ≈ 0.605，`dip` 应该 ≈ `css × 0.605`
2. 如果仍然 `click-no-effect`，看 `window.__diggLog` 是否为空：
   - **空** → 鼠标事件还是没落到按钮上（继续查坐标 / 是否被浮层拦截，可试 `type: "char"` 之外的 `mouseWheel` 或先 hover 200ms 让操作栏展开）
   - **非空但 status_code≠0** → 平台风控/登录态问题，日志会打印 code 与 HTTP 状态
   - **有 status_code=0** → 成功，`liked` 会 +1，`task_runtime_logs` 里应出现真实点赞
3. 成功一次后，把 `nurtureLikePercent` 调回 10%~20% 跑满 3 分钟，确认概率/去重（`warmupLiked` 集合防重复点）都正常
4. 再补「收藏/转发」执行器（配置项已经存在，只是不执行）

---

## 7. 红线（不要破）

1. **不伪造点赞数**：没拿到接口回执或 DOM 证据，就不加 `liked`。宁可日志写"点了但未验证"。
2. **不动生产数据**：`data/lead_system.db` 里除了任务/日志/账号事件，其它表不写。
3. **注入脚本不写正则字面量**，改完必跑 `check_warmup_js.js`。
4. **同一视频不重复点赞**（重复点 = 取消赞，会被判异常），已有 `warmupLiked` 集合，别绕开。
5. 任务被手动停止时记 `stopped`，不要记 `failed`（改过一版，别回退）。

---

## 8. 顺手要清的 P2（不影响主链路）

- 日志里「卡片结构样本」整段 HTML（最长 600 字）应该降级/只出一次，现在很吵
- 每轮只尝试一次详情点赞，其余 `need-detail` 屏被后端算成「无控件」，统计不准，应单列「本轮未尝试」
- `nurture_*` 测试任务记录已积 ~40 条，需要清理
- 养号页缺「视频停留」设置项（配置里 `stayMin/stayMax` 已支持，页面没暴露）
- 新建任务对话框没有「仅保存」，评论获客对话框滚轮滚不动，列表横向溢出
- 实况大屏有个错别字

---

## 9. 相关文档

- `docs/module-map.md` 第七/八/九节（实况黑屏、养号卡住、日志重做 + 引擎钉住）
- `nanxi-ai-leads/backend/app/runtime/runners.py` —— 养号主循环、日志格式、进度事件
- `nanxi-ai-leads/backend/app/runtime/registry.py` —— 线程管理、`Ctx.engine` 钉住
