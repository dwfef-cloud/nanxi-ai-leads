# 对标：GitHub 同类项目怎么处理评论获客的容错与风控

> 调研日期：2026-11-21　适用模块：`nanxi-ai-leads` 评论获客（顺序浏览 / 主评 / 回复）
> 目的：本轮暴露的「跑 2 条就停」「大片视频被发布时间过滤」「模型接口 403」三类问题，看同类项目的成熟做法。

---

## 一、被调研的项目

| 项目 | 规模 | 形态 | 与本系统的可比性 |
|---|---|---|---|
| `NanmiCoder/MediaCrawler` | 71.5k★ | Python + Playwright，抖音/小红书评论采集 | **采集侧最权威**：登录态、请求签名、前置网关拦截、频率控制 |
| `GHkmmm/laizan`（来赞） | 82★，活跃 | Electron + Playwright，抖音信息流自动评论引流 | **产品形态几乎一致**：逐条刷视频 → 判断 → 评论 → 记录 |
| `cs-lazy-tools/ChatGPT-On-CS` | 4.6k★ | 多平台 AI 客服（千牛/抖店/小红书） | 参考多模型接入与消息可靠性，但自动化层非公开 |
| Crawlee（Apify） | 官方库 | 爬虫框架 | 403/被封的**分类重试**与 session 轮转范式 |
| LiteLLM Router | 官方库 | 多模型路由 | 模型侧重试/冷却/备用部署范式 |

---

## 二、问题 1：模型网关前置拦截（HTTP 403 / Cloudflare 1010）

### 同类项目怎么做

**MediaCrawler**（`media_platform/douyin/client.py`）遇到的是同一类问题，只是网关换成抖音的 ArgusSecurityPlugin：

- 缺 `x-tt-argus` → 403 `Blocked by ArgusSecurityPlugin Uifid Not Found`
- 缺 `uifid` cookie → 同样 403
- `uifid` 与 `verifyFp` **必须同源**（都用 cookie 里的值），自生成 verifyFp 会被判 `Signature Not Found`

它的做法是**把网关要求的特征补齐并写进注释**，而不是把它当成鉴权错误反复改密钥：

```python
# 抖音边缘网关 ArgusSecurityPlugin 要求的请求头。网关目前不校验取值，
# 传固定字符串即可；将来若开始真校验，会重新出现 "Signature Not Found"。
DOUYIN_ARGUS_HEADER_VALUE = "1"
self.headers.setdefault("x-tt-argus", DOUYIN_ARGUS_HEADER_VALUE)
uifid = cookie_dict.get("UIFID") or cookie_dict.get("UIFID_TEMP", "")
if uifid:
    self.headers.setdefault("uifid", uifid)
```

**Crawlee 文档**把这套做法抽象成两类原因：IP 被封 vs **浏览器指纹被封**，并给出工具链：
`useFingerprints`（Playwright/Puppeteer 默认开启指纹伪装）、`session.markBad()` / `session.retire()`、以及 Cloudflare challenge 用 **Camoufox + `handleCloudflareChallenge`**。

**LiteLLM Router** 走的是另一条路：不跟 WAF 硬碰，**多部署 + 冷却 + 跨组 fallback**。
`allowed_fails` / `cooldown_time`（429 默认冷却 5 秒）、`num_retries`（429 指数退避、其它错误立即重试）、`order` 小者优先，某部署挂了自动升级到下一个 order。

### 与本系统的对照

| 做法 | 适合本系统？ | 说明 |
|---|---|---|
| 补齐网关特征（浏览器 UA / Accept） | ✅ 已做 | 2026-11-21 已改 `ai_personas._call_chat`，UA 从 `NanxiAILeads/1.0` 换成 Chrome UA，实测 10/10 通过 |
| 瞬时失败重试（3 次，1.5s/3s 退避） | ✅ 已做 | 覆盖 CF 偶发 1010、429、5xx |
| 换掉 WAF 网关（官方直连） | ⭐ 推荐 | 最根本。`base_url` 换 DashScope 兼容端点或 DeepSeek 官方，代码不用动 |
| TLS 指纹伪装（`curl_cffi impersonate="chrome"`） | 备选 | 若必须继续用 znbcode 这类中转站。需新增依赖，当前 venv 未安装 |
| 多部署 fallback（主备两个 base_url） | 建议后续 | 见第五节「主备双通道」 |

---

## 三、问题 2：单条失败把整轮任务带崩

### 同类项目怎么做

**laizan** 的 `src/main/service/feed-ac/index.ts` 是逐条容错的教科书式写法，**没有任何一处异常会终止整轮**：

- 拿不到视频信息 → 记 `未获取到当前视频信息` → 睡 1–3s → `goToNextVideo()` → `continue`
- 命中屏蔽词 → 记录 → 下一条
- 评论发送失败 → 记 reason → 关评论区 → 睡 1–2s → 下一条
- 活跃度不够 → 记 reason → 下一条

**AI 判断失败时**（`_matchRuleGroup`）直接降级为「不匹配」，任务继续：

```ts
try {
  const aiResult = await aiService.analyzeVideoType(videoInfoStr, ruleGroup.aiPrompt)
  currentRuleGroupMatched = aiResult.shouldWatch
} catch (error) {
  this._log('error', `AI规则组 "${ruleGroup.name}" 判断失败: ${error}`)
  currentRuleGroupMatched = false   // 保守跳过，不中断整轮
}
```

`BaseAIService.analyzeVideoType()` 里也是二重兜底：`_request` 返回 null → `shouldWatch:false`；JSON 解析失败 → `shouldWatch:false`。

### 与本系统的对照

本系统 2026-11-21 之前是「生成失败 = 整轮终止」，这也是「2/20 条、互动 0 次」的直接原因。现已改为：

- 单条 `GenerationError` → `GenerationSkipped` → 跳过该视频、按原节奏继续
- **连续 3 条**失败才停轮（`GENERATION_FAIL_LIMIT`）

与 laizan 的差异：**它无限跳过，本系统有熔断**。理由：本系统的单条成本是 70–170 秒的拟人停留，模型全挂时无限跳过会白烧 40 分钟；熔断阈值 3 是同一次实测里 2 条即失败的自然延伸。

是否要把阈值调到 5、或改成「失败 3 条后降级为纯浏览不互动」，按实际体验再定。

---

## 四、问题 3：结果判定与「发了没发」不确定

### 同类项目怎么做

**laizan** 不靠「没抛异常」判断成功，而是**监听评论发布接口**：

```ts
if (url.includes('https://www.douyin.com/aweme/v1/web/comment/publish')) {
  const responseBody = await response.json()
  if (responseBody?.status_code === 0) resolve({ success: true })
  else resolve({ success: false, reason: `status_code: ${responseBody?.status_code}` })
}
// 5 秒内没抓到响应 → 判失败（timeout 后移除监听）
```

**MediaCrawler** 同样用 `response.text == "" || response.text == "blocked"` → 抛 `account blocked`，而不是把空响应当成成功。

风控弹窗：laizan 检测 `.second-verify-panel`，**等人工 60 秒**，超时才报错；期间不重试、不刷请求。

### 与本系统的对照

这块本系统**比多数同类项目更严**：`write_reservations` 的 `sent_ok / uncertain` 状态机 + 读回校验（commentId 匹配、HTTP 200、status 0）已经覆盖了「发了但不确定」的场景，laizan 只记录历史、没有去重与回读。

不用改。唯一可借鉴的是：**把「不确定」原因也进小结**，避免用户只看到「互动 0 次」而不知道有几条是待核验。

---

## 五、问题 4：大量视频被「发布时间超出范围」过滤

这是本轮另一个隐性损耗：16:59 那轮 6 条视频里 5 条是「看了 3–8 秒即划过：视频发布时间超出范围」，真正进入评论区的只有 1 条。

### 同类项目怎么做

laizan 不用「发布时间」筛视频，而是用**评论区活跃度**（`_checkVideoActivity`）：

- 评论数 ≥ 5：前 5 条里至少 2 条是 2 天内 → 值得评论
- 评论数 < 5：至少 1 条在 1 天内 → 值得评论

它的判断依据是「这个视频现在有没有人在看/在聊」，而不是「视频什么时候发的」。对获客来说这个口径更贴近目标：老视频如果评论区还在活跃，同样能触达活人。

另外它**直接监听 feed / comment list 接口的 JSON**（`_setupVideoDataListener`、`_openCommentSectionAndMonitor`），不从 DOM 文案反推发布时间；本系统目前是「详情页没提供发布时间 → 用最早评论时间推断」，属于同一条思路的手工版。

### 建议

1. **筛选口径增加「评论区活跃度」开关**：与现有 `publishWithinDays` 并列，二选一或叠加。
2. **「推断发布时间」从兜底升级为首选字段**：直接取 `ageMinutes` 的最小/最大值分布，而不是先看详情页有没有。
3. **推荐流默认放宽发布时间**：推荐流本身就是「当下在推」的内容，再用 7 天/30 天卡一遍，等于自己把候选砍掉。

---

## 六、可直接落地的四条（按优先级）

| 优先级 | 动作 | 落点 | 成本 |
|---|---|---|---|
| P0 | 模型服务换官方直连或加主备双通道 | `system_settings.ai.base_url` + `_call_chat` 增加备选 endpoint | 低（配置级）/ 中（代码 30 行） |
| P1 | 小结里输出「跳过原因 TOP3 + 待核验条数」 | `leadgen_stream.finish()` 汇总 `live_action_results.code` | 低 |
| P1 | 筛选加「评论区活跃度」口径 | `leadgen_plan` 时间过滤处，参考 laizan `_checkVideoActivity` | 中 |
| P2 | `curl_cffi` 指纹伪装（仅在继续用中转网关时） | `ai_personas._call_chat` 的 HTTP 层 | 中（新增依赖） |

---

## 附：参考坐标

- MediaCrawler：`config/base_config.py`（CDP 模式、`SAVE_LOGIN_STATE`、`MAX_CONCURRENCY_NUM=1`、`CRAWLER_MAX_SLEEP_SEC=2`）；`media_platform/douyin/client.py`（Argus 头、请求签名、`crawl_interval`）
- laizan：`src/main/service/feed-ac/index.ts`（逐条容错、评论接口判定、验证码等待、拟人输入）；`src/main/integration/ai/base.ts`（AI 失败降级）
- Crawlee：`docs/guides/session-management`（`markBad`/`retire`）、`docs/guides/avoid-blocking`（指纹、Camoufox、Cloudflare challenge）
- LiteLLM：`docs/routing`（`num_retries`、`allowed_fails`、`cooldown_time`、`fallbacks`、`order`）
