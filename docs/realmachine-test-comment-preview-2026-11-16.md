# 评论获客真机测试（2026-11-16 晚）

接续同日养号测试（`realmachine-test-nurture-2026-11-16.md`）。本轮范围：评论获客只读预览、评论采集入库、写动作安全门禁。
全程使用正式客户端「阿里山AI获客」+ 本机桥接 8899，账号「噜噜噜」（本地档案「AI学长小马」，抖音）。

## 一句话结论

预览链路原本**在回复模式下根本跑不通**（两个阻断性缺陷，见下），修复后已能真实读取 16 条评论、生成 2 条回复动作、零发送；写安全门禁六层全部按预期拒绝，`write_reservations` 始终 16 未变。

## 环境

- 桌面壳：`desktop/` 下 Electron，窗口「阿里山AI获客」，壳同时负责拉起后端（`backend/.venv`）
- 后端：`127.0.0.1:8899`，`executor online=true, engine=shell`
- 账号：`3732e4b2a76f4d059ee9fea7e7781e9b`，昵称「噜噜噜」
- 测试任务：`leadgen_1794826716254`，关键词「智能体开发」
- 验收范围文件：`data/acceptance/*.json` 均为 `expiresAt=0`（关闭），本轮**未开启任何写窗口**

## 发现并修复的两个阻断问题

### 1（P0）评论解析脚本语法错误，任何评论都读不到

现象：预览返回 `Script failed to execute, this normally means an error was thrown`。

壳日志原文：

```
14:28:09.897 页面控制台: Uncaught SyntaxError: Identifier 'publishedText' has already been declared line=113
14:28:09.899 指令执行出错: read-comments Script failed to execute...
```

原因：`desktop/douyin_agent.js` 的 `COMMENT_EXTRACT_JS` 里同一块作用域内 `const publishedText` 声明了两次，整个注入脚本直接语法错误。影响面是**评论获客、评论点赞、评论监控三条链路全部不可用**，而不只是本模块。

修复：只保留带 `.slice(0, 60)` 的那一次声明。
补测：`_verify/check_warmup_js.js` 原来只校验 `WARMUP_JS / LIKE_PROBE_JS / CARD_PROBE_JS / DIGG_HOOK_JS / INPUT_SPY_JS`，评论脚本从未被求值过——这就是它能活到真机的原因。已把 `COMMENT_EXTRACT_JS`、`SEARCH_ITEMS_JS` 纳入求值校验，并在 `_verify/test_warmup_offline.js` 增加断言「模板求值后必须是合法 JS」（桌面回归 145 → 146）。

### 2（P0）出口合规校验把评论目标当平台 uid 拦下

现象：评论读通后，预览返回 `出口字段疑似平台 uid: commentId`。

链路：`ui/live-bridge.js` → 主进程 `preview-leadgen-live` → 后端 `/ipc/preview-leadgen-live` → `ipc_bridge.ipc_invoke` 里的 `assert_public_safe`。而 `PLATFORM_UID_FIELDS` 含 `commentId`，回复/私信动作必然带它，于是**回复模式永远过不了预览**。这不是测试脚手架的问题，是产品路径的阻断。

修复思路：出口与内部解耦，而不是放宽合规。

- 提案（`live_workflow_proposals`）里继续保留完整 `commentId`，定点回复靠它；
- 出口只给页面「对象 + 原文 + 序号」：新增 `public_actions()`，剔掉 `commentId / commentUserId / userId / videoId / uid / secUid / authorId / openid`，补一个 `ref` 序号；
- 确认窗口（`desktop/leadgen-review.js`）只用 `commentAuthor / sourceText / recipientId / videoUrl / text`，`prepare` 靠 `proposalId + binding` 从库里还原目标——都不需要出口带平台 ID。

回归测试随之调整：凡是要构造签名命令的用例，改为从库里读提案（`stored_actions()`），并新增断言「出口不得出现 commentId」。

## 本轮实测用例

| 用例 | 结果 |
|---|---|
| 主评模式只读预览 | ✅ 6.1s，2 条真实视频 → 3 条主评动作，`mode=live` |
| 回复模式只读预览（开回复+意向词） | ✅ 25.3s，读 16 条评论 → 2 条回复动作，另 1 类动作按「无意向且可准确定位评论」跳过 |
| 出口字段合规 | ✅ 动作里无 `commentId`，有 `commentAuthor / sourceText / ref` |
| 评论采集入库 | ✅ `leads` 868 → 884（+16），`source_task_id=leadgen_1794826716254` 可追溯；重复跑同一视频 `leadsTotal=0`（去重生效，未重复入池） |
| 零写动作 | ✅ `write_reservations` 16 → 16 全程不变；无 post-comment / like / favorite 的新结果行 |
| 无验收范围时写请求 | ✅ 4 个变体（like-video / post-comment / follow-user / 空账号）全部「未配置此账号与任务的真机验收范围」 |
| 有效范围但动作不在白名单 | ✅「此入口仅支持复制链接、收藏和视频点赞」 |
| 有效范围但目标视频越界 | ✅ `blocked / live_writes_disabled`，写明视频不在本次范围 |
| 有效范围但动作次数为 0 | ✅ 同上 |
| 范围过期（expiresAt 已过） | ✅ 按「未配置」拒绝 |
| 范围任务不匹配 | ✅ 按「未配置」拒绝 |
| 已确认清单正文被改 | ✅「动作、正文或收件人与已确认清单不一致，未发送」 |
| 已确认清单回复对象被改 | ✅ 同上 |
| 未确认视频 | ✅「当前账号、任务、视频或动作未包含在本次真机验收范围」 |

写预留计数在以上 13 个用例前后始终为 16，没有任何一次进入浏览器执行。

## 数据证据

| 时刻 | leads | live_action_results | write_reservations | proposals |
|---|---|---|---|---|
| 预览前 | 868 | 82 | 16 | 8 |
| 主评预览后 | 868 | 85 | 16 | 9 |
| 回复预览（脚本报错那次）后 | 868 | 86 | 16 | 9 |
| 回复预览（读通、被合规拦下）后 | 884 | 104 | 16 | 10 |
| 修复后回复预览 | 884 | 122 | 16 | 11 |

`live_action_results` 的增量全部是 `search-video/candidate`、`classify-comment/candidate|skipped` 这类**筛选用候选流水**（`code=筛选通过，尚未执行`），不是写动作结果。

## 需要你决策的三件事

1. **预览阶段就真实入库线索**：`build()` 在没有写动作的情况下也会调 `collector.ingest_comments`，所以「只看一眼没启动」也会让线索池多 16 条。若不想这样，应改成预览只算 `leadsTotal` 预估、确认启动后再入库（现有代码里 `planStats.leadsTotal` 已经是负数预留位，改动集中在一处）。
2. **`recipientId` 其实是平台 sec_uid**，但不在 `assert_public_safe` 的拦截名单里，出口能带出去。确认窗口用它拼 `https://www.douyin.com/user/...`。要么按合规彻底去掉（确认窗口不显示主页链接），要么改名成平台中立字段并显式加白名单。
3. **本轮入库的 16 条测试线索**（`source_task_id=leadgen_1794826716254`）保留还是删掉？这是真实视频下的真实评论，不是假数据；但确实是测试任务产生的。

## 未覆盖

- **真实回复/私信发送**：需要你按具体账号、视频、原评论、正文授权一次，本轮一条都没发。私信页面契约（`desktop/live-dm.js`）仍未用真实账号校准。
- **评论点赞模式**（`like-comment`）：本轮没跑，链路用的是同一个评论读取脚本，脚本修好后可单独验。
- **主评发送**：老任务 `leadgen_1794707862594` 的历史回读仍是 `comment_publish_failed`，不重发。

## 回归

```
backend  .venv/Scripts/python.exe -m pytest -q        # 818 passed
desktop  node --test _verify/test_*.js                # 146 passed
desktop  node _verify/check_warmup_js.js              # 7 个注入脚本求值后均合法
```

## 复现命令

```bash
# 回复模式只读预览（不发送）：payload 取最新提案，再覆盖这几项
python - <<'PY'
import sqlite3, json, urllib.request
c=sqlite3.connect('file:data/lead_system.db?mode=ro',uri=True,isolation_level=None)
p=json.loads(c.execute("select body from live_workflow_proposals order by id desc limit 1").fetchone()[0])['payload']
p.update({'enableVideoComment':False,'enableComment':True,'commentReplyMode':'custom',
          'commentContent':'关于工作流的输入输出，可以具体说说吗？',
          'commentIntentMatchMode':'keyword','intentionKeywords':['工作流','智能体'],
          'interactionLimit':3,'commentMin':3,'commentMax':3,'videoMin':1,'videoMax':2,
          'likeMin':0,'likeMax':0})
req=urllib.request.Request('http://127.0.0.1:8899/ipc/preview-leadgen-live',
  data=json.dumps({"args":[p]},ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req,timeout=280).read().decode()[:800])
PY
```
