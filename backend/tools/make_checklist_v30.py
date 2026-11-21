# -*- coding: utf-8 -*-
"""从 v29 生成 v30：#124 批量跟进的真机验收跑通了，并修掉一路上拦路的壳侧缺陷。

真机验收（2026-11-18 20:52–20:55，账号 1794886618996「AI学长小马」，真实抖音页面）：
1. 只读链路：read-author-works 打开「朝阳.」主页，8.1s 读到 5 条作品，engine=shell、mode=live。
2. 关注：1 账号 + 1 线索 → follow = success，account_events 记 follow_sent（20:53:07，detail 含 lead_id）。
3. 幂等：对同一个人再发一次 → skipped / already_sent，不重复打扰。
4. 首作点赞：read-author-works → 取首作 → like-video = success（20:54）。

真机暴露并修掉的缺陷（这是本次最有价值的发现）：
- 现象：写动作一律被判 live_preview_not_visible，日志是「可交互核验失败（真显示）：hidden/complete」——
  页面已加载完，但 document.visibilityState 恒为 hidden，连 show-worker 把窗口弹出来也过不去。
- 根因：账号的驻留窗是 show:false 的 parking 窗口，showInactive + opacity 1 + 移到屏幕内之后，
  DWM 仍把它判为被遮挡，页面一直是 hidden。
- 修复（desktop/worker-surface.js + douyin_agent.js）：showForCheck 时临时置顶（screen-saver 层）
  并 moveTop 让系统承认这一帧被呈现；核验通过后由新增的 releaseTopmost 立刻取消置顶。
  注意不能 hide —— 页面重新隐藏又变回 hidden，后续 sendInputEvent 会被丢掉。
- 影响面：这条不是 #124 专属，**所有无人值守写动作**（monitor 自动回复、评论获客、批量跟进）
  都走同一个 ensureActionable 门槛，此前在后台窗口下会集体失败。

状态口径：#124 保持「已实现」，备注补真机证据；其余不变。
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v29.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v30.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]

# ── #124：已实现，补真机证据 ─────────────────────────────────────────
o.cell(row=125, column=7).value = (
    o.cell(row=125, column=7).value
    + "。【真机验收 2026-11-18 20:52–20:55，账号 1794886618996】"
      "① 只读链路 read-author-works 打开真实主页 8.1s 读到 5 条作品（engine=shell、mode=live）；"
      "② 关注 1 账号 1 线索 → success，account_events 记 follow_sent（20:53:07，detail 含 lead_id）；"
      "③ 同目标二次执行 → skipped/already_sent（幂等门生效，不重复打扰）；"
      "④ 首作点赞 read-author-works → like-video → success。"
      "验收同时修掉一个壳侧通用缺陷：写动作一律被判 live_preview_not_visible（hidden/complete）——"
      "驻留窗是 show:false 的 parking 窗，showInactive + opacity 1 之后 DWM 仍判遮挡，"
      "页面 visibilityState 恒 hidden；改为核验时临时置顶 + moveTop、通过后 releaseTopmost 立刻放开"
      "（不能 hide，否则页面回 hidden、点击事件被丢）。"
      "该门槛 monitor（自动回复/评论获客）同样要走，此前后台窗口下的写动作会集体失败，属通用链路修复。"
      "仍未真机跑：私信、首作评论（公开发言需用户在场）、分享主页、多账号并行节奏。"
)

# ── 看板追加 v30 ────────────────────────────────────────────────────
dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 20:58　　真机验收（v30）："
    "#124 批量跟进在真实抖音页面上跑通——关注 success（账本记 follow_sent）、同目标二次 already_sent 幂等、"
    "首作点赞 success、只读 read-author-works 8.1s 读到 5 条作品。"
    "过程中修掉壳侧通用缺陷：无人值守写动作因驻留窗被 DWM 判遮挡（visibilityState 恒 hidden）"
    "一律停在 live_preview_not_visible，改为核验时临时置顶 + 通过后 releaseTopmost（不 hide）。"
    "该修复同时惠及 monitor 自动回复与评论获客的写动作。"
    "全量回归 1225 项 = 1222 passed + 3 failed（3 项均为 patchright 未安装，与基线一致）。"
)

wb.save(DST)

# ── 落盘核验 ────────────────────────────────────────────────────────
chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
note = str(w.cell(row=125, column=7).value or "")
print("#124 状态:", w.cell(row=125, column=6).value)
print("  含真机证据:", "真机验收 2026-11-18" in note, "| 含置顶修复:", "releaseTopmost" in note)
print("看板含 v30:", "v30" in (chk["统计看板"]["A3"].value or ""))
