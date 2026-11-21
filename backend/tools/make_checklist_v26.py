# -*- coding: utf-8 -*-
"""从 v25 生成 v26：#5 推荐页随机样本 + 多账号并发互检调度器落地（#5/#11 改判已实现）。"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v25.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v26.xlsx"

wb = openpyxl.load_workbook(SRC)

ws = wb["吞评检测"]
GREEN_FILL_SRC = ws.cell(row=2, column=4)  # 已有的「已实现」绿色样式参照


def mark_implemented(row, note):
    d = ws.cell(row=row, column=4)
    d.value = "已实现"
    d.fill = copy(GREEN_FILL_SRC.fill)
    d.font = copy(GREEN_FILL_SRC.font)
    ws.cell(row=row, column=5).value = note


# #5 样本来源（r6）：改判已实现——推荐页随机样本 + 多账号并发互检调度器
mark_implemented(6, (
    "本地评论：comment_likes.candidates 服务端解析（含失效校验）；推荐页随机：本轮真实开发落地——"
    "每个选中账号在推荐页扫评论数 < 200 的视频（壳侧 recommend-scan 原语，逐屏翻流读评论徽标），"
    "按样本数量在不同视频各发表 1 条（post-comment 逐条签名授权，占用账号跳过换候选）；"
    "样本就绪等待随机 5–10 秒即可与其它已就绪账号互检，无需等全部账号；配对调度器：目标只被检测"
    "一次、不检查自己、单数由先完成的账号检测剩余、账号被其他任务占用则启动即拒绝（不跑一半）；"
    "壳侧执行循环改为按账号并发（同账号串行、跨账号并行，MAX_PARALLEL 默认 4），后端指令队列同账号"
    "互斥、shell-task 独占；10 项单测锁定（配对/单数兜底/三模式/换候选/作用域）；真机验收待跑"))

# #11 样本数量 / 评论模式（r12）：改判已实现——三模式随推荐页样本接通
mark_implemented(12, (
    "每账号样本数量：visibilitySamplesPerTarget 生效（每账号截断，0=不限，上限 100）；评论模式三档"
    "本轮接通（推荐页样本发表时用）：人设 = 按账号绑定的智能体生成（AccountManager 的 accountPersonas "
    "绑定 + ai_personas 通道，生成失败换下一个候选不硬凑）；随机正向表情 = 内置正向表情池随机；"
    "自定义 = 用户填写的固定文案；人设模式启动前校验每个互检账号都已绑定，未绑定直接拦下并指名账号"))

# #4 高速模式代理（r5）：备注补一句推荐页/自检都不经平台接口
ws.cell(row=5, column=5).value = (
    "高速模式已按「自检」语义实现（用登录会话读评论区，不经平台接口），本轮推荐页样本同样走真实"
    "浏览器会话（recommend-scan + 发表），都不经平台接口查询，visibilityProxyExtractUrl 仍无消费方；"
    "待真正接口直采时才需要代理，保持未实现")

# ---------------------------------------------------------------------------
# 其他模块表 87 行：吞评检测汇总行同步改判
# ---------------------------------------------------------------------------
other = wb["其他模块"]
cell = other.cell(row=87, column=6)
cell.value = "已实现"
# 该表历史行里「已实现」有的还是红色填充，这里统一用清单标准绿（与吞评检测表一致）
cell.fill = copy(GREEN_FILL_SRC.fill)
cell.font = copy(GREEN_FILL_SRC.font)
other.cell(row=87, column=7).value = (
    "配置、建单、常规互检、高速自检（backend_api）、推荐页随机样本 + 多账号并发互检调度器、"
    "明细/导出/删除全链路已接通（常规互检 2026-11-17；高速自检与回复样本 2026-11-18；"
    "推荐页样本与并发执行 2026-11-18）；壳侧并发执行与 recommend-scan 需真机验收")

# ---------------------------------------------------------------------------
# 统计看板 A3 追加 v26 版本行
# ---------------------------------------------------------------------------
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 18:38　　本轮更新（v26）：吞评检测表 #5/#11 改判已实现——"
    "推荐页随机样本与多账号并发互检调度器真实落地（壳侧按账号并发执行 + recommend-scan 原语 + "
    "配对调度器 + 评论三模式）；吞评检测表「部分实现」清零；全量 1192 项测试 1189 passed + "
    "3 failed（patchright 未安装环境问题，与基线一致）。")

wb.save(DST)

# 落盘核验：重读确认改判与样式
chk = openpyxl.load_workbook(DST)
w = chk["吞评检测"]
report = []
for row in (5, 6, 12):
    d = w.cell(row=row, column=4)
    report.append((row, str(w.cell(row=row, column=1).value), d.value,
                   d.fill.start_color.rgb if d.fill and d.fill.start_color else None))
print("吞评检测核验：", report)
print("部分实现剩余：", sum(1 for r in range(2, w.max_row + 1)
                         if str(w.cell(row=r, column=4).value or "") == "部分实现"))
o = chk["其他模块"]
print("其他模块 87 行：", o.cell(row=87, column=6).value,
      o.cell(row=87, column=6).fill.start_color.rgb,
      str(o.cell(row=87, column=7).value)[:40])
print("A3 尾行：", chk["统计看板"]["A3"].value.split("\n")[-1][:120])
print("SHEETS:", chk.sheetnames)
