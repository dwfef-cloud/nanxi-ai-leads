# P2 界面修复记录 · 2026-11-16

承接《用户视角体验报告 · 第 2 轮》（user-experience-report-2026-11-14-run2.md）的 P2 清单。
本轮共修 2 项、实证否决 2 项，全部改动只在 `ui/assets/` 三个 bundle，后端零改动。

## 已修复

### 1. 新建评论获客任务没有「仅保存」（P2-1）

- 现象：弹窗底部只有「取 消 / 保存并启动」，想先存草稿必须真的启动任务。
- 根因：创建与启动在 store 里焊死——`startLeadgenTask`（index bundle）先跑账号循环、发 `start-task`，再把 `status:"running"` 的任务记录落库。不存在只落库不启动的入口。
- 修复（两处）：
  - `ui/assets/index-1pPBwPwk.js`：leadgen 任务 store 工厂内新增 `saveLeadgenTaskDraft(m,f)`——用与 start 相同的记录构造器（`la(m)` configSnapshot 归一、`ct(tt)` 记录规整、`z0(m)` 自动命名、`B0()` 零统计），落一条 `status:"stopped"`、`startedAt:null`、`runs:[]` 的任务记录，只调 `save-leadgen-task`（后端 `_save_leadgen_task` 原样落库，天然支持），绝不发 `start-task`。挂到返回对象 `saveLeadgenTaskDraft`。
  - `ui/assets/LeadgenTasks-CjivisWF.js`：弹窗 footer 在「保存并启动」前新增「仅保存」按钮，handler 与 Qa 同款校验（任务名称、可用账号），成功后关弹窗 + toast「任务已保存（未启动），可在列表中用「重启」随时启动」+ 刷新列表；列表里它以「已停止」chip 呈现，「重启」即正常启动（restart 路径读 configSnapshot.selectedAccounts）。

### 2. 养号页没有「视频停留」设置（P2-4）

- 现象：提示词 §A 假设有「视频停留」，页面只有总时长/互动比例；启动 payload 写死 `stayMin:8,stayMax:25`。
- 根因：后端养号 runner（`backend/app/runtime/runners.py:676`）**本来就认** `stayMin/stayMax`（区间 1–60），是前端把它焊死了。
- 修复（两处）：
  - `ui/assets/AccountNurture-DbuYmtts.js`：
    - 养号总时长字段后新增「视频停留（秒，随机区间）」双输入框（min1/max60，复用 `field/range-row/range-sep/range-unit/field-hint` 现成样式），绑定 `nurtureConfig.stayMin/stayMax`，留空显示默认 8/25，随既有 300ms 防抖自动保存（save-nurture-config 全量透传，后端零改动）。
    - 启动 payload 的 `stayMin:8,stayMax:25` 改为读配置 + 钳制（1–60，min>max 自动对调，空值回退 8/25）。
  - `ui/assets/index-1pPBwPwk.js`：`nurtureConfig` 默认对象补 `stayMin:8,stayMax:25`。

## 实证否决（当前构建已不存在）

### 3. 弹窗鼠标滚轮滚不动（P2-2 前半）

- 复测结论：**不可复现**。三种环境实测弹窗主体都能滚：干净 Electron（scrollTop 0→600）、Electron+壳 preload（0→600）、浏览器实测也能滚。`.leadgen-create-modal .ant-modal-body{max-height:72vh;overflow-y:auto}` 规则生效（wrap 上挂类、body 是真滚动容器，sh 1810 / ch 637），也没有任何 wheel preventDefault（capture/bubble 探针 defaultPrevented 全 false）。
- 注意：合成 `dispatchEvent(new WheelEvent())` 不会触发滚动属正常行为，测滚轮必须用可信输入（Electron `sendInputEvent` / CDP）。

### 4. 大屏轨迹错别字（「引挚」「既预定」「相当条评」）（P2-3）

- 全 bundle + 后端源码检索：这些字符串不存在于任何静态文案。大屏轨迹渲染的是运行日志原文，11-14 报告截图里的错字来自当时的日志内容（真实任务名/评论），不是 UI 文案 bug。留观察即可。

### 5. 列表横向溢出 / 名称列被截断（P2-2 后半）

- 列定义 `scroll:{x:1322}`（= 各列宽和 140+150+100+180+56+248+400+48），列宽总和与 scroll.x 一致，不存在压缩；操作列 fixed:right 常驻。1480 宽实测：truncatedCells=0，名称列省略号正常，横向滚动可达全部列。900 窄窗复测同样无截断。
- 11-14 报告的「名称列只剩两三个字」截图复现不出（当时疑为加载瞬态或浏览器缩放伪象）。不动列布局，留观察。

## 验证

- 新增回归：`desktop/_verify/p2_ui_audit.js`（Electron 加载真实 UI @ 8899，隔离档案；**所有写通道拦截**：save-*、start-task 本地应答，get-* 走真实桥；分节安装、用完还原）。断言：
  - 养号页出现「视频停留」及说明文案；
  - 点「开始养号」→ `start-task` payload 带 `stayMin/stayMax`（默认 8/25，钳在 1–60，min≤max，taskMode=nurture），启动被拦截、无平台动作；
  - 点「仅保存」→ `save-leadgen-task` 载荷 `status:"stopped"`、带 configSnapshot、无 startedAt、**无 start-task 调用**、弹窗关闭 + toast；
  - 弹窗滚轮（可信输入）scrollTop 0→600；
  - 大屏无错别字。
  - 结果：`desktop/_verify/p2-ui-audit/result.json`（AUDIT PASS）+ `list.png` / `modal.png` / `modal-footer.png`。
- 既有回归：`check_warmup_js.js` 4 个注入脚本 OK；`leadgen_settings_native.js`（真实 Vue 资产 + 夹具 IPC）PASS；桌面 `node --test _verify/test_*.js` **144/144**。后端未改动。

## 注意事项

- **缓存**：三个 bundle 文件名未变（沿用原 hash 名），改完需重启桌面壳或在界面硬刷新（Ctrl+F5）才会加载新代码——历史 P0 教训（启发式缓存拿旧脚本）在这里同样适用。
- 11-16 21:17 的审计在拦截逻辑补全前，有一次 `save-task-config` 穿透到了生产库（载荷=当时从库里读出的评论获客配置，等价于用户打开一次该页面的自动保存，数据本身无编辑）；此后所有写通道均已拦截，后续运行零穿透。
- 未动的 P2 相关项：29 条 self-warmup IPC 对应前端接线、任务结束后运行状态不自清（P1-1，本轮未复测）等仍待办。
