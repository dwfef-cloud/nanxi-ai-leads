# 获客系统 · 精华版前端（LEGACY · 非交付主线）

> **定位说明（2026-11-13）**
> 真正给最终用户用的界面是 `../ui/`（阿里山壳，桌面壳加载 `/ui/`）。
> 本目录是南溪早期的轻量 SPA，现在只作为**辅助验证面**挂在 `/manage`——
> 用来在不启动桌面壳的情况下快速核对后端数据与页面逻辑。
> 正式交付可设 `LEGACY_MANAGE=0` 关闭该挂载，本目录也可整目录移除。

> 对应 PRD：
> - `deliverables/product-strategy/prd-acquisition-v02-to-v1-improvement-2026-08-25.md`（基础版）
> - `deliverables/product-strategy/prd-douyin-touchpoint-expansion-2026-08-25.md`（触点扩展，IMP-020）
> - `deliverables/product-strategy/prd-lifecycle-deal-closing-2026-08-25.md`（客户经营与成交，IMP-028~034）
>
> 覆盖需求：IMP-007（经营分析）、IMP-009（轻量 SPA）、IMP-004（安全模式 UI）、IMP-005（话术 AB UI）、IMP-011（上手向导）、IMP-020（触点归因）、IMP-028~031（商机阶段/成交录入/企微侧频控/转介绍）、IMP-032（触点归因分析）

## 如何启动

先启动后端 API，再在 `frontend` 目录运行 `python -m http.server 8080`，访问 `http://127.0.0.1:8080`。

## 文件结构

```
frontend/
├── index.html            页面骨架（三栏布局，PRD §7 草图落地）
├── assets/
│   ├── css/style.css     设计系统（配色/字体/组件/响应式/打印）
│   └── js/
│       ├── mock-data.js  Mock 数据层（模拟 API，含接口映射表）
│       └── app.js        路由 + 全部视图 + 交互
└── README.md             本文件
```

## 精华版信息架构

左侧导航只展示日常运营最常用的 4 个模块：

```
工作台          #/workbench     今日要处理什么（指标+预警+待办+动态）
线索池          #/leads         线索筛选、状态与详情
互动中心        #/interactions  评论回复 + 私信收件箱
客户与商机      #/customers     商机阶段与成交管理
```

完整设置通过右上角“配置”按钮进入，不占用主导航模块。旧的扩展页面不再注册前端路由，访问旧书签会自动回到工作台；对应后端 API 仍保留，便于后续按需恢复能力。

## 页面与 PRD 需求对照

| 页面 | PRD 需求 | 关键实现 |
|---|---|---|
| 工作台 | 北极星导向 | 今日指标卡（新线索/待发私信/**今日加微★**/今日成交/待跟进）+ "需要你处理"预警（带跳转）+ 今日待办 Top3 + 实时动态 |
| 线索池 | leads 状态机 + IMP-020 | 九状态筛选 + **来源触点列**（自有评论/竞品评论/主动私信/粉丝私信/转介绍/留资卡）+ 详情抽屉 + 手动标记加微（IMP-002 降级入口） |
| 私信任务 | IMP-001 | 队列状态指标（待发/频控暂缓/失败待恢复/已发待回复/待人工回复）+ 发送队列表格 + 进行中对话 |
| 客户与商机 | IMP-028/029/031 | 商机阶段管线卡（added→measured→proposal→quoted→negotiating→won/lost，可点击筛选）+ 客户抽屉（阶段步进器/跟进 timeline）+ **成交录入 ≤3 次点击** + 流失原因沉淀 + 转介绍客户（referral 来源）示例 |
| 今日跟进 | IMP-028 | 逾期（红色警示）/今天/明天及以后 三段式待办，checkbox 勾选完成 |
| 转化漏斗 | IMP-007 | 六级漏斗 + 单位成本 + ROI + 14天加微趋势 + "一个加微值 ¥xx" |
| 触点归因 | IMP-020/032 | 6 触点贡献表（内联 bar，与漏斗数据对齐）+ 内容榜 TOP5 视频 + 话术贡献 |
| 账号健康度 | IMP-006/007 | 0-100 健康分（≥60绿/<60黄/<40红联动安全模式）+ 五维因子 + R1 降速标注 |
| 合规风险 | IMP-007/010 | 退订/黑名单增速/投诉 + R1/R2/R3 规则引擎 + 审计事件流 |
| 话术库 | IMP-005 / R2 | 主话术+变体统计，R2 命中自动停用；配置弹窗含**企微侧频控**（IMP-030） |
| 抖音账号 | IMP-006 | 健康分卡 + limit_status 状态机 + 安全模式详情 |
| 上手向导 | IMP-011 | 5 步：绑账号→选行业话术→设频控→启动→看结果 |
| 全局安全模式 | IMP-004 | 顶部红色横幅 + 全量暂停 + **退出需人工确认**（写审计日志） |

## 后端对接方式（后续接后端时）

`mock-data.js` 中所有函数均返回 Promise，签名与 REST 接口一一对应（见该文件底部 `MOCK_API_MAP` 注释），新增域包括：

- `getWorkbench()` → `GET /api/workbench`（今日指标+预警）
- `getDmQueue()` → `GET /api/dm/queue`（私信队列）
- `getCustomers()` / `getStageMeta()` → `GET /api/customers`
- `advanceStage(id, stage)` → `POST /api/customers/{id}/stage`
- `recordDeal(id, amount)` → `POST /api/customers/{id}/deal`
- `markLost(id, reason)` → `POST /api/customers/{id}/lost`
- `getFollowups()` / `completeFollowup(id)` → `GET/POST /api/followups`
- `getAttribution()` / `getSourceMeta()` → `GET /api/analytics/attribution`

对接时只需：

1. 将 `mock-data.js` 中各函数体替换为 `fetch()` 调用；
2. `app.js` 与 `style.css` **零改动**；
3. 运行态类接口（安全模式/暂停）对应 `POST /api/compliance/safe-mode/enter|exit`、`/api/compliance/pause|resume`。

## 设计说明

- **基调**：克制稳健的"合规安全感"——暖纸底 + 松绿主色 + 墨色文字，避免炫技 BI 感；老板要"一眼看懂 + 一键暂停"。
- **无依赖**：零框架零 CDN（PRD S3 风险条："SPA 技术选型轻量，避免过度工程"），后续如迁 Vue/React 可平移此信息架构。
- **可访问性**：语义化标签、aria-current/role/aria-live、键盘可关弹窗、焦点可见、prefers-reduced-motion 支持。
- **响应式**：<860px 导航折叠为顶部横条，漏斗/健康行自适应。
- 所有演示态写操作（新建话术/绑号/向导提交）仅回显 toast 提示"未落库"，不做持久化。
