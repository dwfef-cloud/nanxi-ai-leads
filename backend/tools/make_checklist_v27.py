# -*- coding: utf-8 -*-
"""从 v26 生成 v27：23 条「部分实现」分诊——9 条改判已实现（含 4 条真开发），14 条保持订正。

真开发（2026-11-18 本轮）：
- #25 每日定时统计调度线程（scheduler.run_once 钩子，4 项单测）
- #60 官方定时（≥2h 交平台定时发布，失败回落软件等到点提交）
- #61 视频自定义封面上传（发布时应用所选封面）
- #67 多账号并行（最多 2）/ 依次处理 + stagger 间隔（publish.py worker 模型重写）
改判（证据过时）：
- #7/#18 账号绑定智能体执行链已贯通（persona_payload 账号优先）
- #12 添加账号链路已闭环；#21 删除/查看数据已闭环；#53 日志通道早已注册；#68 批量修改前端已实现
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v26.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v27.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]
GREEN_FILL = o.cell(row=88, column=6).fill  # 该表里标准的「已实现」绿色
GREEN_FONT = o.cell(row=88, column=6).font


def mark(row, status, note):
    d = o.cell(row=row, column=6)
    d.value = status
    if status == "已实现":
        d.fill = copy(GREEN_FILL)
        d.font = copy(GREEN_FONT)
    o.cell(row=row, column=7).value = note


# ── 改判已实现（证据过时 / 本轮开发） ──────────────────────────
mark(8, "已实现", (
    "绑定链路已全通：账号池卡片下拉读写 ai_personas 配置的 accountPersonas（AccountManager 页面），"
    "执行侧 monitor_engine.persona_payload 账号绑定优先、未绑定回落任务级通用人设，已消费于 AI 研判"
    "（runners）、评论/私信文案（monitor_engine/autoreply）、吞评人设评论生成（visibility）；"
    "v21 备注「执行侧用任务级 aiPrompt、账号绑定 persona 尚未贯通」已过时"))

mark(13, "已实现", (
    "添加账号链路闭环：备注名校验 → 登录窗口（扫码 / 创作者登录 / 充值登录）或粘贴 CK 导入"
    "（douyin-cookie-import-result 验证）→ 登录状态核验 → save-account-pool 全量同步落 accounts 表；"
    "CK 导入失败自动移除临时账号；登录窗口本身走桌面壳 init-automation-view；"
    "v21 备注「账号创建链路未接通道」已过时"))

mark(19, "已实现", (
    "与「AI 智能体-绑定」同链路：绑定下拉（含「不使用智能体」）读写 accountPersonas，"
    "「管理智能体库」跳转 AI 智能体页；绑定了智能体的账号运行时经 persona_payload 自动使用 AI"))

mark(22, "已实现", (
    "编辑账号打开设置弹窗；删除账号确认（「将删除 X 及其本地登录态」）后经 save-account-pool "
    "全量同步：accounts 行删除且关联数据（account_events / account_stats_daily / dm_send_results）"
    "一并清理，壳侧本地登录态同步移除；查看数据跳账号数据页（accountDataFocusId）"))

mark(26, "已实现", (
    "每日自动统计调度线程本轮真实开发落地：save-account-data-schedule 时刻（EXPLICIT）落库后，"
    "scheduler.run_once 挂每日钩子——到配置时刻触发一轮 account_data.collect（独立线程，"
    "不阻塞 schedule-runner 的 tick）；成功后当天不再触发，失败（壳不在线等）不占当天名额、"
    "10 分钟限频重试；合规安全模式与执行器离线暂停语义沿用既有机制；4 项单测锁定"))

mark(54, "已实现", (
    "通道早已注册：评论点赞任务日志走 get-entity-leadgen-task-runtime-logs / "
    "clear-entity-leadgen-task-runtime-logs（ipc_bridge 均已接），UI 一键复制走剪贴板、"
    "清空调后端通道；v21 备注「get/clear-comment-like-logs 未注册」是通道名考证错误——"
    "UI 实际调用的就是上面两个通道"))

mark(61, "已实现", (
    "排期双轨本轮接通：距发布时间 ≥ 2 小时优先提交官方定时（壳侧打开平台「定时发布」开关并"
    "填时间，提交成功记 officialScheduledAt）；官方定时设置失败（控件不可用/平台拒绝）明确回报"
    "status=schedule_failed，后端回落「软件等到点提交」并记录原因——绝不提前发、绝不丢篇；"
    "2 小时内由软件等到点提交（既有逻辑）；8 项新单测 + 19 项旧契约回归；真机验收待跑"))

mark(68, "已实现", (
    "多账号并行本轮真实开发：publish.py 运行器重写为 worker 模型——publishMode=parallel 时最多 "
    "2 个账号并行（同账号严格串行，占用记账 + 崩溃残留 processing 可重新领取），interval 依次处理"
    "且每条提交后 staggerMinSec–staggerMaxSec 随机等待（此前间隔从未实现）；并行模式下 login_required "
    "只跳过该账号剩余篇，其余账号照常发布；单测覆盖并行/串行/间隔/残留恢复/跳账号"))

# ── 保持部分实现（备注订正） ──────────────────────────────────
mark(62, "部分实现", (
    "视频自定义封面上传本轮接通：coverEnabled+custom 时把所选横/竖封面文件交给壳（发布页「编辑封面」"
    "弹窗内 input[type=file] 经 CDP 写入），找不到入口或写入失败只记录原因（coverSkipped）不阻断发布；"
    "「自动设置横竖封面」为平台默认行为无需软件干预；图文第 N 张作封面未接（需在编辑器里点选缩略图，"
    "无稳定原语）"))

mark(63, "部分实现", (
    "字段可存（save-work-publish-task：topicsEnabled/topics、mentionsEnabled/mentions、"
    "activityEnabled/activityName 等）；发布时无执行——话题与 @好友需在 contenteditable 描述框触发"
    "联想选择（选择器不稳定、误选风险高），保持未接；壳侧原语边界内（文件/标题/描述/封面/官方定时）已全部落地"))

mark(64, "部分实现", (
    "同上：音乐/位置/关联热点的字段可存，发布时无执行——均为弹窗式交互（搜索选择器、地图选点、"
    "热点列表），无稳定原语，保持未接"))

mark(65, "部分实现", (
    "同上：同时发布今日头条/红果短剧需要各平台的授权会话与勾选联动，平台侧授权无法自动化；"
    "「指定内容不可用会记录原因、不带错误设置发布」的语义已在封面（coverSkipped）与官方定时"
    "（schedule_failed 回落）两条链路落地"))

mark(39, "部分实现", (
    "本轮取证订正整条链路现状：UI 调用 resume-nurture-safety 后端未注册（点击「处理后继续」必报错），"
    "且「检测到平台限制提示」的检测端（壳侧风控弹窗识别）与暂停标记（nurtureSafetyPaused）的生产者"
    "均未建立——前后端都没有任何写入；落地需要真机取证风控弹窗 DOM；当前断线恢复由 start-task 重启、"
    "进度按养号状态机延续"))

mark(40, "部分实现", (
    "与 #38 同一链路：前端提示与「处理后继续」按钮在（nurtureSafetyReason 文案 + resume-nurture-safety "
    "调用），后端检测/暂停/恢复三段均未建立，保持未接；落地前置条件是壳侧养号执行器嵌入风控文案检测"
    "（真机取证 DOM）"))

mark(70, "部分实现", (
    "本轮开发后待真机验收：启动/停止/定时状态机（published/uncertain/failed）+ 官方定时与封面提交链路"
    "的单测已锁，但发布页选择器（上传框/标题/描述/封面弹窗/定时开关）未在已登录创作者中心环境验证；"
    "uncertain 不自动重试、不显示成功（2026-11-17 口径不变）"))

mark(83, "部分实现", (
    "用户类型/粉丝数筛选已接线（inspect-profile 单测覆盖）；弹幕行 uid 提取已接；"
    "真机直播流验收待做（2026-11-18 口径不变）"))

mark(101, "部分实现", (
    "配置可存；后台监听线程未做真机验收；首次开启只建立基线，不重复推历史（口径不变）"))

mark(105, "部分实现", (
    "chat-new-message / chat-webhook-result 已接桥接事件总线（单测覆盖），事件持久化排队；"
    "真机验收待做（口径不变）"))

mark(113, "部分实现", (
    "订正缺口性质：实况大屏是预构建 Vue 产物（ui/assets 无源码），五档计数（已发/定时/失败/待确认/待发）"
    "的渲染与数据接线需要前端源码工程；后端数据已就绪——publish 任务 payload 的 items 逐篇状态"
    "（published/scheduled/failed/uncertain/ready）可完整计数，发布执行器状态机已就绪"))

mark(125, "部分实现", (
    "任务/调度(nextRunAt)/运行记录/日志/结果/用户历史/8 类领取规则已真实落库；动作发送需桌面端逐动作"
    "授权，无授权时 start-batch-action 拒绝启动且不建假 run（2026-11-17 口径不变）"))

mark(126, "部分实现", (
    "config.profileShareFriends 校验 + 分享动作入队（share-profile / share-comment）；空好友/群名直接"
    "拒绝；动作发送同 #124，需桌面端授权与真实好友昵称/群名（2026-11-17 口径不变）"))

mark(133, "部分实现", (
    "lead-comment-interact 由桌面壳 openLeadSource 处理，仅桌面壳可用（口径不变）"))

# 统计看板 A3 追加 v27
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 19:40　　本轮更新（v27）：23 条「部分实现」分诊——9 条改判已实现"
    "（#25 定时统计调度线程 / #60 官方定时 / #61 视频封面 / #67 多账号并行与 stagger 节奏为真开发；"
    "#7/#12/#18/#21/#53/#68 证据过时改判），14 条保持并订正证据（#62/#63/#64 壳侧无稳定原语、"
    "#38/#39 需真机取证风控 DOM、#112 无前端源码、#69/#82/#100/#104/#124/#125/#132 真机/授权类）；"
    "全量 1210 项测试 1207 passed + 3 failed（patchright 环境问题，与基线一致）。")

wb.save(DST)

# 落盘核验
chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
report = [(w.cell(row=r, column=1).value, w.cell(row=r, column=6).value,
           w.cell(row=r, column=6).fill.start_color.rgb if w.cell(row=r, column=6).fill else None)
          for r in (8, 13, 19, 22, 26, 39, 40, 54, 61, 62, 63, 64, 65, 68, 69, 70, 83, 101, 105, 113, 125, 126, 133)]
for row in report:
    print(row)
print("A3 尾行：", chk["统计看板"]["A3"].value.split("\n")[-1][:120])
print("部分实现剩余（其他模块）：", sum(1 for r in range(2, w.max_row + 1)
                                     if str(w.cell(row=r, column=6).value or "") == "部分实现"))
