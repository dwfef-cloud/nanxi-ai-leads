# -*- coding: utf-8 -*-
"""从 v24 生成 v25：吞评检测表 4 条核查（#2/#6 改判已实现，#5/#11 保持订正）。"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v24.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v25.xlsx"

wb = openpyxl.load_workbook(SRC)

# ---------------------------------------------------------------------------
# 吞评检测表
# ---------------------------------------------------------------------------
ws = wb["吞评检测"]

GREEN_FILL_SRC = ws.cell(row=2, column=4)  # 已有的「已实现」绿色样式参照


def mark_implemented(row, note):
    d = ws.cell(row=row, column=4)
    d.value = "已实现"
    d.fill = copy(GREEN_FILL_SRC.fill)
    d.font = copy(GREEN_FILL_SRC.font)
    ws.cell(row=row, column=5).value = note


# #2 检测方式（r3）：改判已实现——高速模式按 UI「自检」语义真实开发
mark_implemented(3, (
    "常规互检（window）已接 visibility.py 引擎（2026-11-17）；高速模式（backend_api）"
    "本轮真实开发落地——UI 文案证明其语义是「自检」而非平台接口（「只检查勾选的本地评论，"
    "不需要互检账号」），原「无平台评论查询接口」的前提不成立：被检账号用自己的登录会话读"
    "自己视频评论区（每视频最多 200 条，样本含回复时展开楼中楼），按 kind 分开核对主评/回复；"
    "账号被其他任务占用记检测未完成，绝不猜结论（visibility.py _run_selfcheck，7 项单测锁定）；"
    "真机验收待跑"))

# #4 高速模式代理（r5）：保持未实现，备注订正前提
ws.cell(row=5, column=5).value = (
    "高速模式已按「自检」语义实现（用登录会话读评论区，不经平台接口），"
    "visibilityProxyExtractUrl 当前无消费方；待真正接口直采时才需要代理，保持未实现")

# #5 样本来源（r6）：保持部分实现，备注订正缺口
ws.cell(row=6, column=5).value = (
    "local 经 comment_likes.candidates 服务端解析（含失效校验）；recommend 保持部分实现"
    "但备注订正：原「发表评论执行器未接通」已过时——post-comment 执行器已接通"
    "（live_leadgen.perform，监控首作/获客链路在用）；真缺口是多账号并行互检调度"
    "（「所选账号同时运行、样本就绪即互检」需要任务级协同器，当前运行时一次只跑一个账号），"
    "启动即显式拒绝")

# #6 评论类型（r7）：改判已实现——回复样本本轮接通
mark_implemented(7, (
    "主评：candidates() 产出 post-comment sent_ok 记录；回复：本轮接通——reply-comment "
    "sent_ok 回执产出 kind=reply 样本（reply: 键前缀，主评旧键不变，旧保存的选择不受影响），"
    "检测按样本类型分开核对（主评只比主评、回复只比楼中楼，互不误判）；首作：首作评论的发表"
    "记录同为 post-comment，本就随主评列出，原「回复/首作记录不补造」备注过时已订正"))

# #11 样本数量 / 评论模式（r12）：保持部分实现，备注订正
ws.cell(row=12, column=5).value = (
    "每账号样本数量：visibilitySamplesPerTarget 已在引擎生效（每账号截断样本，0=不限，"
    "UI 在推荐页模式提供输入）；评论模式（人设/表情/自定义）订正：前端配置已在"
    "（visibilityCommentMode persona/emoji/custom + visibilityCustomComment），它是推荐页"
    "样本发表时的文案模式，随 #5 的推荐页互检调度器一并落地；人设生成体系已在"
    "（ai_personas，monitor_engine.persona_payload 可复用）")

# ---------------------------------------------------------------------------
# 其他模块表 86 行：吞评检测汇总行备注同步订正
# ---------------------------------------------------------------------------
other = wb["其他模块"]
other.cell(row=87, column=7).value = (
    "配置、建单、常规互检引擎、高速自检（backend_api）、明细/导出/删除全链路已接通"
    "（常规互检 2026-11-17、高速自检与回复样本 2026-11-18）；推荐页样本需多账号并行"
    "互检调度器，启动即显式拒绝")

# ---------------------------------------------------------------------------
# 统计看板 A3 追加 v25 版本行
# ---------------------------------------------------------------------------
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 17:55　　本轮更新（v25）：吞评检测表 4 条「部分实现」核查——"
    "2 条改判已实现（#2 高速模式：UI 文案证明语义是「自检」而非平台接口，本轮真实开发落地："
    "被检账号用自己的会话核对勾选样本，7 项单测；#6 回复样本：reply-comment 回执进入候选、"
    "按 kind 分开核对，首作备注过时订正）；2 条保持部分实现并订正证据（#5 推荐页样本真缺口是"
    "多账号并行互检调度器而非发表执行器；#11 评论模式随 #5）；#4 代理与「其他模块」汇总行"
    "备注同步订正；统计变化：吞评检测表部分实现 4→2")

wb.save(DST)

# ---------------------------------------------------------------------------
# 落盘核验：重读确认状态、样式与版本行
# ---------------------------------------------------------------------------
wb2 = openpyxl.load_workbook(DST)
ws2 = wb2["吞评检测"]
for r, no in ((3, "#2"), (5, "#4"), (6, "#5"), (7, "#6"), (12, "#11")):
    c4 = ws2.cell(row=r, column=4)
    print(no, repr(c4.value), c4.fill.start_color.rgb,
          repr(str(ws2.cell(row=r, column=5).value)[:60]))
print("其他模块 r87:", repr(str(wb2["其他模块"].cell(row=87, column=7).value)[:80]))
print("A3 lines:", wb2["统计看板"]["A3"].value.count("\n") + 1)
print("last line head:", wb2["统计看板"]["A3"].value.split("\n")[-1][:60])
print("OK")
