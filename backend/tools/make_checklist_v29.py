# -*- coding: utf-8 -*-
"""从 v28 生成 v29：P0 三条（#124 授权链路、#38/#39 风控接口、#69 静态契约）本轮落地。

本轮做了什么（都可核对）：
1. #124 批量跟进真正能执行了：新增 app/runtime/batch_runner.py —— 点「开始执行」时把
   「账号 + 跟进方式 + 线索清单」固化成一次性作用域（mode=batch），逐条走
   live_leadgen.perform 签名后交给壳；关注/私信/首作互动/主页分享都有执行路径，
   结果逐条落 batch_follow_items，失败原因不外编。壳侧同步放开 batch 模式下的
   目标校验（主页地址）、按目标计数、分享主页不再要求 reviewedActions。
2. #38/#39 把 resume-nurture-safety 注册上了（幂等、只清标记），并加了暂停状态的
   存取与查询通道，界面不再「点了必报错」。检测端仍未实现（要真机取证风控页面）。
3. #69 补了一组前端↔后端契约测试：发布页请求通道全部有后端路由、推送事件不被当路由、
   后端读取的发布字段都有发送方、定时时间路径（item.schedule.runAt）两侧一致。

状态口径：
- #124 由「部分实现」改为「已实现」——代码闭环 + 10 项单测；真机验收另列。
- #38/#39 保持「部分实现」，但缺口从「接口未注册」缩小为「检测规则未实现」。
- #69 保持「部分实现」（真机项），备注补上已完成的静态校验范围。
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v28.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v29.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]
GREEN_FILL = copy(o.cell(row=89, column=6).fill)   # #88 已实现行的绿底
GREEN_FONT = copy(o.cell(row=89, column=6).font)

# ── #124 批量跟进：已实现 ─────────────────────────────────────────────
d = o.cell(row=125, column=6)
d.value = "已实现"
d.fill = GREEN_FILL
d.font = GREEN_FONT
o.cell(row=125, column=7).value = (
    "本轮打通执行端（此前 start-batch-action 恒返回「动作执行器未接通」）："
    "① 授权即「用户点开始执行时选定的账号+跟进方式+线索清单」，执行器（app/runtime/batch_runner.py）"
    "把它固化成 mode=batch 的一次性作用域，动作集合与次数上限按线索数给，多一条都签不出来；"
    "② 每条命令仍逐条走 live_leadgen.perform 签名，壳侧按 账号/任务/目标/次数上限 复核"
    "（live-platform 放开 batch 模式的主页地址目标 + 按目标计数，live-social 分享主页不再要求 reviewedActions）；"
    "③ 动作覆盖：首作点赞/收藏（按概率）、首作评论（AI 或话术，先落 reviewedComments 再签）、"
    "关注、私信、分享主页；④ 结果逐条落 batch_follow_items（成功/失败/跳过 + 原因），"
    "run 的 total/current/success/failed/skipped 实时累加，get-batch-runtime-state 供大屏读；"
    "⑤ 关注/私信/首作评论写 account_events 与 dm_send_results，触达账本与「已触达」筛选随即生效；"
    "⑥ 新增 stop-batch-action（已开始的跑完、未领取的不再领）；桌面壳离线不启动、不建假 run。"
    "单测 10 项（tests/test_batch_runner.py）锁定。真机验收另列：真实页面上的关注/私信/首作互动的最终落点")

# ── #38 / #39 风控：接口已通，检测未实现 ─────────────────────────────
o.cell(row=39, column=7).value = (
    "本轮修掉最硬的一段：resume-nurture-safety 已注册（幂等、无副作用，只清暂停标记并记恢复时间），"
    "「处理后继续」不再报错；同时新增暂停状态的写入/查询（app/runtime/nurture_safety.py，"
    "nurture_safety 表 + get-nurture-safety 通道），谁检测到就写谁。"
    "仍未实现：检测端（壳侧识别风控弹窗/toast/验证码页）。原因不变——提示形态多且文案会变，"
    "规则写宽会把正常浏览误判成风控无故暂停，写窄则账号已被限流仍在跑；须真机撞一次限制取证 DOM 后才能落地。"
    "断线恢复仍由 start-task 重启、进度按养号状态机延续")
o.cell(row=40, column=7).value = (
    "与 #38 同一链路，本轮把「恢复」这一段补齐（接口 + 状态表 + 查询通道），按钮点击有真实回执；"
    "保留未接的是「平台限制提示」的检测：nurtureSafetyPaused / nurtureSafetyReason 目前只能由外部写入，"
    "壳侧养号执行器尚未嵌入风控文案识别。落地前置条件仍是在真机上取证风控页面结构")

# ── #69 作品发布：静态契约已锁，真机待验 ────────────────────────────
o.cell(row=70, column=7).value = (
    "本轮补静态可验证部分（tests/test_publish_ui_contract.py，5 项）："
    "① 发布页 13 个请求通道在后端全部有路由；② work-publish-task-event / work-publish-view 是壳→UI 推送事件，"
    "确认不被误注册成路由；③ 后端读取的 13 个发布字段（publishMode/staggerMin/staggerMax/封面三件套/"
    "runAt/schedule/items/...）在打包资源里都有发送方；④ 定时时间路径两侧一致（item.schedule.runAt），"
    "避免「界面填了时间后端读不到」这类不报错的漂移；⑤ imageCoverIndex 仍为已知未接字段（图文第 N 张封面，归 #62）。"
    "仍未做：真机验收（上传框/标题/描述/封面弹窗/定时开关的实际选择器）、uncertain 不自动重试口径不变")

# ── 看板追加 v29 ────────────────────────────────────────────────────
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 20:40　　P0 落地（v29）："
    "① #124 批量跟进由「部分实现」→「已实现」：新增 batch_runner（一次性 batch 作用域 + 逐条签名 + "
    "首作互动/关注/私信/主页分享 + 结果与触达记账 + stop-batch-action），壳侧放开 batch 目标校验；"
    "② #38/#39 resume-nurture-safety 已注册（幂等）并新增暂停状态存取/查询，按钮不再报错，"
    "检测端仍需真机取证，故保持「部分实现」；"
    "③ #69 补前端↔后端契约测试 5 项（通道/字段/定时路径/已知未接字段），真机验收仍待跑。"
    "全量回归 1225 项 = 1222 passed + 3 failed（3 项均为 patchright 未安装的环境问题，与基线一致）。")

wb.save(DST)

# ── 落盘核验 ────────────────────────────────────────────────────────
chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
for it in (38, 39, 69, 124):
    print(f"#{it}", w.cell(row=it + 1, column=6).value,
          "|", str(w.cell(row=it + 1, column=7).value)[:60])
print("看板含 v29:", "v29" in (chk["统计看板"]["A3"].value or ""))
