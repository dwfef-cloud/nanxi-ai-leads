# 部署手册（D27）

> 目标：一台干净的 Windows 机器，**30 分钟内**跑起来并看到真实数据。
> 全部命令在 `nanxi-ai-leads/backend` 目录下执行（除特别注明）。
> 本手册值已在 2026-11-13 实际环境验证：Python 3.12.14 / fastapi 0.162 / patchright 1.64.4（chromium 156）。

---

## 一、前置条件

| 项 | 要求 | 检查命令 | 说明 |
|---|---|---|---|
| 操作系统 | Windows 10/11 x64 | — | 桌面壳（Electron）必需；纯浏览器模式可跨平台 |
| Python | 3.12.x | `python --version` | 装时勾选 Add to PATH |
| Node.js | 18+（可选） | `node -v` | **仅桌面壳需要**；只用浏览器模式可跳过 |
| 磁盘 | ≥ 3 GB | — | chromium 约 400 MB，库与日志约 2 GB 余量 |
| 端口 | 8899 空闲 | `netstat -ano \| findstr 8899` | 桥接服务固定端口 |
| 屏幕 | ≥ 1366×768 | — | 桌面壳默认 1440×900 |

---

## 二、30 分钟部署清单

### 步骤 1 · 落盘（2 min）

**整个 `nanxi-ai-leads` 目录是一个自包含单元**，拷到任意路径都能跑（含中文路径、含换盘符）：

```
nanxi-ai-leads\
├── backend\      # FastAPI 桥接服务 + 三个 sqlite + venv
├── ui\           # 前端界面（阿里山壳 15 页，由后端挂在 /ui）
├── desktop\      # Electron 桌面壳（加载 /ui，自动拉起 backend）
├── frontend\     # LEGACY：辅助验证面，挂在 /manage，正式交付可关
├── docs\         # 交付文档
└── logs\
```

> 路径含中文没问题，但**不要**放在 `C:\Program Files` 或任何需要管理员权限写入的目录：
> 后端要写 `data/`、`logs/`，权限不足会静默失败。
>
> 旧版本要求 `ui`、`desktop` 在别的盘（`D:\workbuddy\...`），已改为随包分发，
> 不再需要任何外部路径配置。

### 步骤 2 · 建虚拟环境（3 min）

```bat
cd /d <交付包>\backend
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -r requirements.txt
```

预期：最后一行出现 `Successfully installed ...`，无红色 ERROR。

### 步骤 3 · 装浏览器内核（5 min，需联网）

```bat
.venv\Scripts\python -m patchright install chromium
```

预期：`Chromium 156.0.8078.4 downloaded to ...`。
失败多半是网络：设代理后重试，或从已部署机器整目录拷贝
`%LOCALAPPDATA%\ms-playwright\chromium-1248\`。

### 步骤 4 · 数据落位（2 min）

确认三个库存在：

```
backend\data\lead_system.db      # 线索/账号/任务/会话（交付包已含历史数据）
backend\data\execution.db        # 执行层：候选池/配额/发送日志
backend\data\account_pool.db     # 账号状态机/风控事件
```

全新空环境（无历史库）时，首次启动会自动建表；此时页面为空属正常，
不要用假数据填充——**空态优于假数据**（DESIGN-DOC §D26 验收线）。

### 步骤 5 · 渲染层路径（0 min，无需配置）

后端默认读**同包内的 `../ui/`**，随包分发，拷到哪都不用改：

```
nanxi-ai-leads\ui\index.html
```

只有一种情况需要覆盖——你要用**别处的新版构建产物**调试：

```bat
set RENDERER_DIR=D:\your\path\dist-protected\renderer
```

启动日志出现 `renderer: builtin` 表示走默认 `../ui/`，`external` 表示走了覆盖路径。

### 步骤 6 · 启动后端（1 min）

```bat
.venv\Scripts\python -m uvicorn bridge_server:app --host 127.0.0.1 --port 8899
```

看到 `Application startup complete.` 即成功。**不要关闭这个窗口**。

> 为什么不用 `app.main:app`：其 lifespan 会自动拉起 MediaCrawler 子进程并做自动备份，
> 属于 §0.5 红线。`bridge_server.py` 只挂载路由，不起子进程。

### 步骤 7 · 健康检查（1 min）

```bat
curl http://127.0.0.1:8899/ipc/health
```

预期（关键字段）：

```json
{"success":true,"mode":"live","rendererExists":true,"dbExists":true,
 "explicitChannels":43,"emptyChannels":41,"blockedChannels":14,"viewAdapterChannels":12}
```

判定：
- `mode` 必须是 `live`；若是 `demo` 说明渲染层没找到 → 回步骤 5
- `dbExists` 必须 `true`
- `emptyChannels` 有值不算故障（见 §六 FAQ 3）

### 步骤 8 · 全通道契约验收（2 min）

```bat
cd /d <交付包>\backend
.venv\Scripts\python tools\bridge_check.py check
```

预期：`94/94 通道应答、0 uid 泄漏、14 个危险通道禁用、PASS`。

### 步骤 9 · 打开界面（1 min）

浏览器：`http://127.0.0.1:8899/ui/`

首次进入有两个弹窗，属预期：
1. **协议弹窗** → 勾选后点「同意并继续」，否则所有导航点击无响应
2. **授权中心**（未激活时）→ 关闭即可，不影响读数据

### 步骤 10 · 授权（1 min，可选）

未激活时页面可读但 `aiQuota=0`，22 个写通道保持「功能未开通」。本地演示码：

```bat
python bridge.py demo-code          # 生成 24h 演示码
python bridge.py activate <码>      # 激活
python bridge.py license            # 查看状态
```

激活后**刷新页面**，状态由 `check-auth` 通道反馈给前端。

---

## 三、桌面版（可选）

```bat
cd /d <交付包>\desktop
npm start
```

依赖与 Electron 运行时已随包（`desktop\node_modules`），**无需 npm install、无需联网**。
桌面壳启动会自己检查 8899 是否在跑，不在则用 `backend\.venv` 自动拉起，再加载 `/ui/`。

> 目录已合并：旧版把桌面壳放在 `D:\workbuddy\...\desktop-shell`，现已归入交付包 `desktop\`，
> 只有一份，`main.cjs` 与 `src/`（南溪原版壳）已移除。

桌面版与浏览器版的差异：

| 能力 | 浏览器 | 桌面版 |
|---|---|---|
| 15 个页面读数据 | ✅ | ✅ |
| 创作聚合 / 消息聚合 | iframe 嵌入 | 原生 BrowserView（更稳、可带登录态） |
| 账号登录（扫码） | ❌ | ✅ |
| 账号资料自动同步 | ❌ | ✅（打开创作聚合时只读抓 header） |

> **Electron 启动坑**：若从某些 IDE/终端启动，环境里可能有 `ELECTRON_RUN_AS_NODE=1`，
> 桌面壳会退化成纯 node 进程。启动前清掉：`env -u ELECTRON_RUN_AS_NODE npm start`（bash）
> 或 `set ELECTRON_RUN_AS_NODE=`（cmd）。

---

## 四、目录与端口

```
nanxi-ai-leads/
├─ backend/
│  ├─ bridge_server.py        # 启动入口（uvicorn bridge_server:app）
│  ├─ requirements.txt        # 依赖
│  ├─ app/                    # 业务代码（api / execution / engagement / nurture）
│  │  └─ paths.py             # 可搬路径：DATA_DIR / EXPORT_DIR / BACKUP_DIR / DB_PATH
│  ├─ data/                   # sqlite 库 + exports/ + backups/（勿删）
│  ├─ logs/                   # uvicorn 日志、压测与演练报告
│  └─ tools/                  # 运维脚本（压测、演练、状态归一、体检）
├─ ui/                        # 前端界面（阿里山壳，后端挂在 /ui，随包分发）
├─ desktop/                   # Electron 桌面壳（唯一一份，加载 /ui）
├─ frontend/                  # LEGACY：辅助验证面（/manage），可关可删
└─ docs/                      # 交付文档（部署 / 参数 / 应急预案 / 版本说明）
```

**可搬约定**：数据、导出、备份、界面全部相对包内路径解析，`nanxi-ai-leads/` 整目录
拷到任何机器 / 任何盘符都能直接运行。需要改位置时用环境变量覆盖：
`DATA_DIR`、`LEADS_DB_PATH`、`EXPORT_DIR`、`BACKUP_DIR`、`RENDERER_DIR`。

| 端口 | 用途 | 备注 |
|---|---|---|
| 8899 | 桥接服务（`/ui`、`/ipc/*`、`/api/*`、`/admin/*`） | 主端口，唯一必须 |
| 8000 | 南溪原始 REST | **不监听**；前端已改同源，不要回退到 8000 |

### 授权与运营后台（/admin）

给**发行方**用的独立后台，与业务三库隔离（独立 `data/admin_console.db`）：

- 入口：`http://127.0.0.1:8899/admin/`
- 首次启动自动生成口令，写在 `data/admin_initial_password.txt`——登录后请改密并删除该文件
- 功能：密钥签发（HMAC 签名、离线可校验、含套餐与到期日）/ 客户机监控（心跳判定在线）/
  用量统计 / 操作审计
- 客户机接入：调 `POST /admin/api/activate`（绑定机器）+ `POST /admin/api/heartbeat`
  （每 5 分钟上报用量），后台「接入说明」页有现成报文
- **交付给客户时**：设 `ADMIN_ENABLED=0` 关闭本后台（客户机器上不该带发码能力）；
  `data/admin_secret.key` 是签名密钥（0600），只留在发行方环境，不要随客户包分发
- UI 验收：`tools/verify_admin.py`（无头登录 + 全页截图 + 生成/清理密钥）

---

## 五、升级与回滚

升级：
1. 停后端（Ctrl+C）
2. 备份 `data/`（`xcopy data data.bak-YYYYMMDD /E /I`）
3. 覆盖代码目录（保留 `data/`、`.venv/`）
4. `pip install -r requirements.txt`（依赖有变时）
5. 起服务 → 跑步骤 7、8

回滚：停服务 → 用备份覆盖 `data/` → 用上一版代码目录覆盖 → 起服务。

> 库结构升级走 `ExecutionStore.initialize()`（幂等建表），不会删列。
> 涉及数据订正时用 `tools/` 下的脚本，且默认只预演、`--apply` 才写库。

---

## 六、常见问题（都是实际踩过的）

**1. 页面全空、数字全 0**
先查 `/ipc/health`。`mode=demo` = 渲染层路径错；`dbExists=false` = 库路径错。
桥接层故障时前端返回**空态**而不是假数据，这是设计行为。

**2. 点了菜单没反应**
协议弹窗没关。它要求必须先勾选 checkbox，且**点文字无效**，必须点方框本身。

**3. `emptyChannels=41` 是不是坏了**
不是。这 41 个通道已映射到真实聚合源（runtime_logs / running_task_ids / schedules 等），
空是因为库里没有正在运行的任务。判据是页面能否渲染，不是这个计数。

**4. 创作聚合 / 消息聚合黑屏**
浏览器模式下是 iframe 嵌入，需 `index.html` 的 CSP 允许 `frame-src https: http:`（已修）。
若被改回 `'none'` 会全黑。桌面版走 BrowserView，不受影响。

**5. 账号登录一直「正在拉取环境」**
桌面壳未接管 `send` 通道（前端部分页面用 `send` 而非 `invoke`）。
检查 `live-bridge.js` 是否覆盖了 `send`，以及主进程日志是否出现「登录视图复用预热实例」。

**6. 侧边栏账号名和抖音昵称不一致**
`accounts.name` 是**用户备注**，`nickname` 才是抖音真实昵称。
打开一次创作聚合会自动同步（`/api/accounts/profile-sync`）。

**7. 后端启动报端口占用**
`netstat -ano | findstr 8899` 找 PID → `taskkill /PID <pid> /F`，再起。

**8. `&` 分离启动后服务莫名消失**
Windows 下后台进程会随 shell 退出而死。用受管后台任务（本仓库统一用
`run_in_background` 或 `start /b` 守护），不要用 `cmd &`。

---

## 七、部署后验收（3 条即可）

```bat
:: 1. 通道全通
python bridge.py check

:: 2. 全量回归（应 337 passed / 0 failed）
cd /d ...\nanxi-ai-leads\backend
.venv\Scripts\python -m pytest -q

:: 3. 一键体检（编译 + 合规扫描 + 单测 + 通道 + 库）
.venv\Scripts\python tools\healthcheck.py
```

三条全绿 = 部署完成。任一条红，先看 `logs/uvicorn.err.log`。
