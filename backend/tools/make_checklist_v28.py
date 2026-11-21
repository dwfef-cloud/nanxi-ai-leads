# -*- coding: utf-8 -*-
"""从 v27 生成 v28：修正 v27 的口径错误，并补判 #68。

v27 的两个不一致（自查发现）：
1. 看板 A3 把「#61 视频封面」算进「9 条改判已实现」，但表里该行仍是「部分实现」
   —— 封面确实本轮真开发，但图文第 N 张作封面未接，应保持部分实现；
2. 看板 A3 把「#68 批量修改」列为「证据过时改判」，但 v27 脚本从未给该行下结论。

v28 处理：
- #68 补判「已实现」并给出 UI 取证（WorkPublish 包三种批量模式 + save-work-publish-task 落库）；
  同时如实标注「话题」只落字段、发布时仍不执行（受 #62 限制，不算 #68 的缺口）。
- 看板追加 v28 行，重述口径：9 条改判已实现（#25/#60/#67 真开发 + #7/#12/#18/#21/#53/#68 证据过时）、
  #61 真开发但保持部分实现、13 条保持订正 = 23 条。
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v27.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v28.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]
GREEN_FILL = copy(o.cell(row=88, column=6).fill)
GREEN_FONT = copy(o.cell(row=88, column=6).font)

# ── #68 批量修改：补判已实现（sheet 行 69 = 条目 68） ──────────────────
d = o.cell(row=69, column=6)
d.value = "已实现"
d.fill = GREEN_FILL
d.font = GREEN_FONT
o.cell(row=69, column=7).value = (
    "UI 已具备三种批量模式（WorkPublish 包 payload 取证）：①「账号分配」——把所选队列项改派到勾选账号，"
    "「复制给账号」开关决定是「复制一份」还是「移走」，重复素材按 账号+素材 去重并明确报错"
    "「目标账号已有所选素材，未修改队列」；②「话题」——批量写 optionOverrides.topicsEnabled/topics；"
    "③「标题/描述」——可只填空值（不覆盖已有文案）或强制覆盖，应用前校验禁词/长度并给出原因。"
    "三种模式均改队列后经 save-work-publish-task 落库（原 v26 备注「前端队列操作」即此，非缺口）。"
    "附带说明：话题只落字段，发布时仍不执行（受 #62 限制，不另计为 #68 缺口）")

# ── 看板追加 v28 口径修正 ────────────────────────────────────────────
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 19:58　　口径修正（v28）：v27 两处不一致已订正——"
    "①「#61 视频封面」是真开发，但图文第 N 张作封面未接，保持「部分实现」，不再计入改判；"
    "②「#68 批量修改」v27 漏下结论，本轮取证 WorkPublish 包后补判「已实现」（账号分配/复制给账号、"
    "话题、标题描述三模式 + save-work-publish-task 落库）。"
    "23 条最终口径：9 条已实现（#25 定时统计调度的、#60 官方定时、#67 多账号并行与 stagger 节奏为真开发；"
    "#7/#12/#18/#21/#53/#68 证据过时改判）、#61 真开发但部分实现、13 条保持部分实现并订正证据"
    "（#62/#63/#64 壳侧无稳定原语、#38/#39 需真机取证风控 DOM、#112 无前端源码、"
    "#69/#82/#100/#104/#124/#125/#132 真机/授权类）。")

wb.save(DST)

# ── 落盘核验 ────────────────────────────────────────────────────────
chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
items = [7, 12, 18, 21, 25, 38, 39, 53, 60, 61, 62, 63, 64, 67, 68, 69, 82, 100, 104, 112, 124, 125, 132]
done, part, other = [], [], []
for it in items:
    r = it + 1
    st = w.cell(row=r, column=6).value
    if st == "已实现":
        done.append(it)
    elif st == "部分实现":
        part.append(it)
    else:
        other.append((it, st))
print("已实现", len(done), done)
print("部分实现", len(part), part)
print("其他", other)
print("看板行数含 v28:", "v28" in (chk["统计看板"]["A3"].value or ""))
print("标题校验:", w.cell(row=69, column=1).value, w.cell(row=69, column=2).value, w.cell(row=69, column=3).value)
