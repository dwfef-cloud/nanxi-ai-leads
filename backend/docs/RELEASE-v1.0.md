# v1.0 交付说明与验收结论（D28）

- 生成时间：2026-11-13
- 代码基线：DESIGN-DOC v1.4.7
- 体检结论：`tools/healthcheck.py` → **6 项全绿**
- 回归基线：**337 passed / 0 failed**

---

## 一、交付范围

### 已完成（代码可运行、有单测、真数据对接）

| 模块 | 排期 | 落点 | 验收方式 |
|---|---|---|---|
| 渲染层桥接（M0） | D5–D7 | `app/api/ipc_bridge.py`、`bridge_server.py`、`live-bridge.js` | 94/94 通道应答、0 uid 泄漏、15 页遍历零报错 |
| 执行层骨架 | D1–D4 | `app/execution/`（params/compliance/read_model/store/rules/scorer/screen_reader/write_safety/pipeline） | 55 项单测 + 698 条真实样本噪声回归 |
| 互动模块（M4） | D8–D14 | `app/engagement/`（engine/quotas/humanize/ai_comment/workers） | 29 项单测；patchright 真实驱动链路已通 |
| 频控与双通道 | D17 | `app/engagement/dm_guard.py` | 模拟超频：闸门拦截 100% |
| 养号（M8） | D20–D21 | `app/nurture/`（params/dice/machine） | 28 项单测；掷骰误差 <2% |
| 编排器（M1） | D22 | `app/execution/orchestrator.py`、`app/api/pipeline_api.py` | 杀进程重启后从断点续跑 |
| 账号池 v2（M6） | D23 | `app/engagement/account_pool.py` | 崩溃 30s 租约回收、状态机全路径 |
| 管理面收尾 | D26 | `app/api/execution_dashboard.py`、`app/api/aggregate.py` | 阿里山壳 15 页全真数据、零 console 报错 |
| 触达统计真实化 | D26 补 | `app/execution/touch_stats.py` | 13 项单测；剔除 19 条模拟发送记录 |
| 部署文档集 | D27 | `docs/DEPLOY.md` / `PARAMETERS.md` / `EMERGENCY.md` | 30 分钟部署清单 + 8 条已知坑 |
| 压测/演练工具 | D24/D25 | `tools/stress_run.py`、`tools/risk_drill.py` | simulate 3×4h PASS；演练 8/8 PASS |
| 归档与体检 | D28 | `tools/healthcheck.py`、`tools/normalize_lead_status.py` | 6 项体检全绿 |

### 未完成（外部资源阻塞，非代码缺口）

| 项 | 阻塞原因 | 已就绪的部分 | 解除条件 |
|---|---|---|---|
| D15 A4 收消息验证 | 需真实私信会话 | 参数与通道已固化 | 提供测试账号 |
| D16 企业号 OAuth | 需企业号资质 + 开放平台应用 | `dm_guard` 频控已写死官方值 | 资质下来即可接 |
| D19 协议兜底验证 | 需先决策是否启用兜底 | 兜底默认关、双通道路由已实现 | 业务决策 |
| D24 真实长跑 | 需 3 个有基础权重的账号 | 压测工具已可 `--mode live` | 提供账号 |
| D25 真实演练 | 需真实账号人为触发 | 演练 8 项断言沙盘全 PASS | 提供账号 |
| B 组评论直连 | 需 2–3 个测试账号 | 写入安全三态已移植 | 提供账号 |

---

## 二、TEST-CHECKLIST 结论（A–E）

| 组 | 内容 | 结论 | 说明 |
|---|---|---|---|
| A1 | 环境与前置条件 | ✅ 通过 | patchright 1.64.4 + chromium 156 可启动 |
| A2 | 建会话（cmd=609） | ⏸ 未执行 | 需企业号/协议环境；兜底通道当前未启用 |
| A3 | 发送私信（cmd=100） | ⏸ 未执行 | 同上；官方主路径未接（D16） |
| A4 | 收消息（WS） | ⏸ 未执行 | 需真实会话（D15） |
| A5 | 节奏与风控 | ✅ 参数已固化 | §5.3 五项写死，闸门拦截实测 100% |
| B | 评论直连接口 | ⏸ 未执行 | 需测试账号 |
| C | 写入安全语义移植 | ✅ 完成 | `sent_ok/sent_fail/uncertain` 三态 + 不重试 + 人工确认 |
| D | 风险登记 | ✅ 完成 | DESIGN-DOC §八 |
| E | 参考仓库清单 | ✅ 完成 | `refs/` |

> 「未执行」≠ 失败：这些项的代码路径已实现并有单测，缺的是真实平台环境。

---

## 三、交付包结构

**自包含**：整目录拷到任何机器 / 任何盘符都能直接运行，无需外部路径配置。

```
nanxi-ai-leads/
├─ backend/
│  ├─ bridge_server.py          启动入口
│  ├─ requirements.txt          依赖清单
│  ├─ app/
│  │  ├─ paths.py               可搬路径（DATA_DIR/EXPORT_DIR/BACKUP_DIR/DB_PATH）
│  │  ├─ api/                   REST + IPC 桥接（ipc_bridge/aggregate/execution_dashboard/...）
│  │  ├─ execution/             执行层（采集/筛选/编排/触达账本/合规）
│  │  ├─ engagement/            互动层（驱动/配额/频控/账号池/私信）
│  │  └─ nurture/               养号（参数/掷骰/状态机）
│  ├─ data/                     三个 sqlite 库 + exports/ + backups/（勿删）
│  ├─ docs/                     DEPLOY / PARAMETERS / EMERGENCY / 本文件
│  ├─ logs/
│  │  ├─ stress/<时间戳>/        压测报告（json + md）
│  │  └─ drills/<时间戳>/        演练记录（json + md）
│  ├─ tests/                    337 项回归
│  └─ tools/
│     ├─ healthcheck.py         一键体检（6 项）
│     ├─ bridge_check.py        全通道契约验收（94 通道）
│     ├─ verify_ui.py           无头页面验收（渲染 + 零 console 报错）
│     ├─ stress_run.py          D24 压测
│     ├─ risk_drill.py          D25 风控演练
│     └─ normalize_lead_status.py  状态归一（默认只预演）
├─ ui/                          前端界面（阿里山壳 15 页，后端挂 /ui）
├─ desktop/                     Electron 桌面壳（唯一一份，含 electron 运行时）
├─ frontend/                    LEGACY：辅助验证面（/manage，LEGACY_MANAGE=0 可关）
└─ docs/                        交付文档

包内路径约定：渲染层 `../ui`、数据 `backend/data`、导出 `backend/data/exports`、
备份 `backend/data/backups`，均可用环境变量覆盖。
```

---

## 四、已知限制（明确知情接受）

1. **线索池「共 N 条 / 全选(N)」不随筛选变化**
   该数字取自 `get-leads-pool-stats` 的池快照，渲染在受保护的构建产物里，无法改。
   服务端 `get-leads-page.total` 与 `get-lead-ids` 均为筛选后真实值，数据侧正确。

2. **70 条中文状态未清洗**
   已确认根因：南溪 CRM 客户阶段（`customers.stage`）被误写进 `leads.status`。
   归一工具已就绪（`tools/normalize_lead_status.py`，默认只预演），
   **等待业务确认映射口径后一条命令执行**。

3. **点赞/关注/主页评论恒为 0**
   本地无这三类动作的记录源。已如实返回 0 并在 note 标注，不臆造。
   接入真实互动后，`account_events` 出现 `like_sent/follow_sent` 事件即自动生效。

4. **真实链路一次都没跑过**
   `lead_candidates=0`、`comment_send_log=0`、`nurture_sessions=0`。
   六套执行代码单测全绿但未端到端实跑，这是 v1.0 最大的剩余风险，
   需要测试账号才能解除（D24/D25/D15/D16）。

5. **存量测试会写生产库**
   `pytest` 全量跑一次会把 `leads` 增加数行（当前 945→948）。
   属测试夹具历史问题，桥接层与工具本身对生产库只读。

6. **CRO 机审未做**
   `open-code-review` 需要模型凭据。当前审查结论来自契约扫描 + 单测 + 实测。

---

## 五、交付后第一步建议

1. 按 `docs/DEPLOY.md` 起服务，跑 `python tools/healthcheck.py` 确认 6 项全绿
2. 提供 2–3 个测试账号 → 立即解除 D24/D25/D15/B 组四项
3. 确认中文状态归一口径 → 执行 `normalize_lead_status.py --apply --yes`
4. 决策是否启用协议兜底（D19）→ 不启用则可把 D19 从排期移除
