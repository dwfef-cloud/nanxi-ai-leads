# 参数全表（D27）

> 单一事实来源：`app/execution/params.py`。本文是它的可读版本。
> **变更流程**：评审 → 回写 DESIGN-DOC §5 → 改 `params.py` → 跑 `tests/test_execution_w1.py`。
> 禁止凭感觉很调——这些值来自阿里山活体观测，改动直接影响账号安全。

---

## 一、读屏采集（M2 · `ReadParams`）

| 参数 | 值 | 含义 | 可调 |
|---|---|---|---|
| `max_items_per_second` | 3 | 读取上限，换算为固定最小间隔 | 否（合规限速） |
| `scroll_step_min_px` / `max_px` | 400 / 800 | 滚动步长随机区间 | 否 |
| `scroll_wait_min_ms` / `max_ms` | 600 / 1500 | 步间停留 | 否 |
| `page_dwell_min_s` / `max_s` | 8 / 25 | 单页阅读停留 | 否 |

> 速度预期 **1–3 条/秒**，这是接受值，不要为了快而调高。

---

## 二、互动（M4 · `EngagementParams`）

| 参数 | 值 | 含义 |
|---|---|---|
| `comment_cooldown_min_s` / `max_s` | 300 / 600 | 两次评论间隔（5–10 分钟） |
| `comment_probe_min_s` / `max_s` | 12 / 13 | 定位探测耗时区间 |
| `input_box_recompute_each_time` | `True` | 输入框坐标**每次实时算，不缓存** |
| `ai_submit_pause_min_s` / `max_s` | 20 / 27 | 输入完到按发送的停顿 |
| `keystroke_delay_min_ms` / `max_ms` | 80 / 200 | 逐字输入延迟 |

> `ENABLE_HUMANIZED_PACING` 默认 **False**：本轮只使用固定限速，不启用拟人随机化。
> 是否开启属合规决策，需评审后改。

---

## 三、养号（M8 · `NurtureParams`）

| 参数 | 值 | 含义 |
|---|---|---|
| `watch_min_s` / `max_s` | 8 / 25 | 匹配视频的观看时长 |
| `like_probability` | 0.10 | 点赞概率 |
| `favorite_probability` | 0.10 | 收藏概率 |
| `share_probability` | 0.10 | 转发概率 |
| `open_comments_probability` | 0.32 | 观看中进评论区概率 |
| `comments_browse_limit` | 20 | 评论区最多浏览条数 |
| `pre_end_interaction_window_s` | 14 | 视频结束前多少秒内才互动 |
| `profile_entry_probabilities` | 0.08 / 0.10 / 0.06 | 划走后进作者主页概率（三档） |
| `muted` / `autoplay` / `loop` | `True` / `False` / `False` | 静音、不自动播、不循环 |
| `short_video_max_s` | 15 | 短视频判定阈值 |
| `short_video_seek_s` | 0.05 | 短视频拖动位置 |
| `max_consecutive_skips` | 4 | 连续跳过多少次触发整页刷新（反停滞） |

**互动为独立掷骰**：点赞/收藏/转发各自 10%，互不影响（不是"三选一"）。
**AI 研判失败即结束本轮养号**——与阿里山"降级为无脑刷"刻意不同，宁可少养不可乱养。

---

## 四、私信频控（M5 · `DmFrequencyLimits`，官方值，**写死不可放开**）

| 参数 | 值 | 说明 |
|---|---|---|
| `non_follower_per_person` | 3 | 非互关：3 条/人 |
| `per_hour_people` | 40 | 1 小时 40 人（按收件人去重） |
| `per_day_people` | 100 | 1 天 100 人 |
| `session_window_hours` | 48 | 会话窗口 |
| `session_window_messages` | 6 | 窗口内最多 6 条 |

> 这五项是平台规则，**任何配置都不能放大**。闸门独立于编排器（双保险）。

---

## 五、筛选（M3 · `FilterParams`）

| 参数 | 值 | 含义 |
|---|---|---|
| `intent_threshold` | 70 | 意向分 ≥70 入线索池（管理面可调） |
| `llm_timeout_s` | 10 | 模型超时，超时降级为规则层结果并标 `ai_degraded` |
| `min_effective_chars` | 2 | 过短文本直接丢弃（噪声过滤） |

噪声过滤实测：698 条真实样本 → 过滤 697 条、保留 1 条，「111」类 100% 命中。

---

## 六、账号池（M6 · `account_pool.py`）

| 参数 | 值 | 含义 |
|---|---|---|
| `RISK_WARNING_THRESHOLD` | 3 | 风险积分达 3 → `risk_warning` |
| `COOLING_HOURS_CAPTCHA` | 2.0 | 验证码冷却时长 |
| `COOLING_HOURS_403` | 4.0 | 403 风控拦截冷却时长 |
| 租约心跳 | 30 s | 超时即视为崩溃，租约自动回收 |

状态机：`healthy → cooling → risk_warning → suspended`
- `cooling`：到期**读时自动恢复**，无需人工
- `suspended`：**只能人工恢复**（`manual_recover`），永不自动解除

---

## 七、配额日限额（`QuotaPolicy`）

| 指标 | 默认日限 | 说明 |
|---|---|---|
| `read` | 600 | 可调（非红线） |
| `like` | 150 | 可调 |
| `comment` | 50 | 可调 |
| `dm` | 100 | **官方锁定**，等于 `per_day_people` |

记账口径：快照对账 `delta = max(0, 平台累计 - 基线)`。
平台观测值覆盖本地记账——本地丢计数不会导致超额。

---

## 八、数据保留（`TTL_DAYS`）

| 项 | 值 | 说明 |
|---|---|---|
| 候选池 TTL | 90 天 | 过期清理，`raw_page` 从不持久化 |

---

## 九、运行期环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `LEADS_DB_PATH` | `data/lead_system.db` | 线索库路径 |
| `RENDERER_DIR` | 阿里山 renderer 绝对路径 | 渲染层位置 |
| `DEEPSEEK_API_KEY` | 空 | 模型供应商 1（首选） |
| `ARK_API_KEY` | 空 | 模型供应商 2（兜底） |
| `ARK_MODEL` | `doubao-pro-32k` | 方舟模型名 |

双供应商策略：任一有 key 即可工作；两者皆空时规则层照常跑，标记 `ai_degraded`。
