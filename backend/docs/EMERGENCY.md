# 应急预案 / 改版降级 SOP（D27）

> 四条主链路各有独立降级路径。原则：**先止损（停动作），再定位，最后恢复**。
> 任何情况下不允许「为了跑通而绕过频控/合规闸门」。

---

## 总览

| 编号 | 场景 | 触发信号 | 止损动作 | 恢复判据 |
|---|---|---|---|---|
| E1 | 抖音 DOM 改版 | 选择器失效告警、评论/点赞定位超时 | 停互动与养号 | 选择器修好并跑通单账号冒烟 |
| E2 | 官方接口下线/变更 | 私信接口 4xx/5xx 连续失败 | 停私信，保留读取 | 官方公告确认新版本并回归 |
| E3 | 账号风控/封禁 | 验证码、403、连续 uncertain | 自动 cooling + 停止派发 | 冷却到期或人工确认后恢复 |
| E4 | 模型供应商故障 | AI 超时、`ai_degraded` 上升 | 自动降级规则层 | 供应商恢复或切另一家 |

---

## E1 · 抖音 DOM 改版（发生概率：必然）

**表现**
- `screen_reader` 抛 `SelectorExpired`（选择器失效），日志出现 `selectors.yaml` 相关告警
- 互动 worker 报「输入框未找到」「评论按钮未找到」
- 采集任务产出的候选数骤降到 0

**立即做（5 分钟内）**
1. 停互动与养号：`POST /api/engagement/pool` 把相关账号置 `manual_suspend`，
   或直接停编排器 `POST /api/pipeline/{run_id}/stop`
2. 确认损失范围：查 `comment_send_log` 最近 30 分钟状态分布；
   有 `uncertain` 的**不要重试**，等人工核对页面是否已发出（防重复评论）
3. 保留证据：截图 + 页面 HTML 片段存 `logs/incidents/<时间戳>/`

**定位**
1. 打开 `app/execution/selectors.yaml`，按告警里的 key 逐条手工比对页面
2. 只改**配置**，不改代码（这就是选择器配置化的目的）

**恢复**
1. 改 `selectors.yaml` → 重启后端
2. 单账号冒烟：`python tools/stress_run.py --accounts 1 --hours 0.05 --mode simulate`（链路自检）
   → 有测试账号时再跑 `live` 单账号 10 分钟
3. 冒烟通过后逐个账号解除 `manual_suspend`（**一次只放一个**，观察 30 分钟）

**若改版过大（页面结构重构）**
直接停用互动与养号，只保留读屏采集的搜索页路径；等阿里山侧更新观测参数后
按 §5 流程重新固化，不要临场猜参数。

---

## E2 · 官方私信接口下线/变更

**表现**：企业号开放 API 返回 4xx/5xx，或返回体缺字段。

**立即做**
1. 私信通道切「只读」：不新建会话、不发送
2. **不要**急着开协议兜底（兜底默认关，且需先过 TEST-CHECKLIST A 组）
3. 已在 `uncertain` 的记录挂起，不重试

**定位**
- 查开放平台公告栏；确认是临时故障还是版本下线
- 对比 `dm_send_results.message` 的错误体，判断是鉴权、频控还是接口不存在

**恢复**
- 官方恢复 → 直接继续
- 接口版本变更 → 改 `services/dm_sender_service.py` 的适配层，跑
  `tests/test_engagement_w2.py` + 单条真实发送验证
- 长期不可用 → 走兜底决策流程（需合规评审 + A2/A3 验证），**不是运维能单方面决定的事**

---

## E3 · 账号风控 / 封禁

**表现**：验证码弹层、HTTP 403、发送结果连续 `uncertain`、页面提示"操作频繁"。

**自动处置（系统已有，无需人工）**

| 事件 | 状态迁移 | 时长 |
|---|---|---|
| `captcha` | → `cooling` | 2 小时 |
| `send_fail_403` | → `cooling` | 4 小时 |
| `send_uncertain` ×3 | → `risk_warning` | 累计 |
| `risk_warning` 期间再出风险 | → `suspended` | 需人工 |

**人工介入点**
1. `risk_warning`：**降低该账号配额**（like/comment 减半），观察 24 小时
2. `suspended`：先查 `account_risk_events` 最近事件，确认是否真封号
   - 真封号 → 保留记录，不恢复
   - 误判 → `manual_recover` 前必须人工登录确认账号正常
3. 同一批账号 ≥2 个同时进入 `risk_warning` → 视为**环境级风控**，
   立即全停 24 小时，检查是否 IP/设备指纹关联

**绝不做**
- 换 IP 继续跑
- 短时间内反复 `manual_recover`（会把 `risk_warning` 直接推到 `suspended`）
- 用别的账号顶替同一批目标（关联风险）

**演练**：`python tools/risk_drill.py`（沙盘 8 项断言，秒级；`--inplace` 才动真实池）

---

## E4 · 模型供应商故障

**表现**：`ai_degraded=true` 比例上升，评论/筛选质量下降。

**自动处置**：10s 超时 → 降级规则层结果 → 打标 `ai_degraded`，流程不中断。

**人工介入**
1. 检查是哪家：`scorer` 的 `attempts` 字段会记录尝试顺序
2. 有另一家 key → 直接切（代码里已是双供应商顺序尝试）
3. 两家都挂 → 保持规则层运行；**不要在无模型时放开评论发送**
   （AI 生成失败时 `ai_comment` 返回 `None`，worker 会跳过该条，这是正确行为）

---

## 附：紧急停止全部自动化（一页 SOP）

```bat
:: 1. 停编排器（协作式停止，不丢断点）
curl -X POST http://127.0.0.1:8899/api/pipeline/<run_id>/stop

:: 2. 全部账号置暂停
curl -X POST http://127.0.0.1:8899/api/engagement/pool -H "Content-Type: application/json" ^
  -d "{\"action\":\"suspend\",\"account_id\":\"<id>\",\"reason\":\"emergency\"}"

:: 3. 确认无租约（应返回空列表）
curl http://127.0.0.1:8899/api/engagement/pool

:: 4. 保留现场
xcopy data logs\incidents\<时间戳>\data /E /I
```

判定恢复的前提：**根因已定位 + 单账号冒烟通过 + 观察 30 分钟无新风险事件**。三者缺一不可。

---

## 事故记录模板

```markdown
## 事故 YYYY-MM-DD HH:MM
- 类型：E1/E2/E3/E4
- 影响：涉及账号 N 个、任务 M 个、时间窗 H 小时
- 触发信号：（日志原文一行）
- 止损动作与时点：
- 根因：
- 恢复动作与时点：
- 遗留风险 / 待办：
- 是否需要改参数或代码：（是则走评审流程）
```
