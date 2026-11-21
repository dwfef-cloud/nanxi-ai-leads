# -*- coding: utf-8 -*-
"""从 v30 生成 v31：批量跟进/首作互动的真机验收结果，以及一路上修掉的 4 个壳侧缺陷。

真机验收（2026-11-18 21:00–21:40，账号 1794886618996「AI学长小马」+ 3732e4b2…「噜噜噜」）：
- 关注：success，account_events 记 follow_sent；同目标二次执行 skipped/already_sent（幂等）。
- 双账号并行：4 条线索 4/4 success，两账号首条相隔 4s、第二批也相隔 2s，总跨度 20.3s
  （串行跑完至少 60s），同账号两条之间 18s（动作耗时 + 3–8s 随机间隔）。
- 首作点赞：read-author-works → like-video → success。
- 首作评论：post-comment → success（话术「这条讲得挺实在的。」）。
- 私信：**未通**。点「私信」打开的是消息列表（面板文本是 55 条历史会话），
  没有进入与该用户的对话，需在抽屉里搜索并选定用户，属多步交互，本轮未实现。

真机暴露并修掉的 4 个壳侧缺陷（都是通用链路，不只影响批量跟进）：
1. 驻留窗（show:false 的 parking 窗）被 DWM 判遮挡 → document.visibilityState 恒 hidden
   → 所有无人值守写动作停在 live_preview_not_visible。修：核验时临时置顶 + moveTop，
   通过后 releaseTopmost 立刻放开（不能 hide，否则页面回 hidden、点击事件被丢）；
   并给冷启动留 450/650ms 呈现时间（实测冷启动第一条常失败、第二条就过）。
2. 用户主页有两个文案为「私信」的按钮（一个是 0×0 隐藏节点），而判定要求「全局恰好 1 个」
   → 每次私信必然 not_verified。修：先按可见尺寸过滤再要求唯一。
3. 私信面板 class 名漂移（实测是 CSS Modules 的 componentsRightPanel*，
   conversation-panel / chat-panel 全不存在）。修：按右侧抽屉尺寸（宽 240–720、高≥200）
   取最外层祖先，不再依赖 class 名；收件人核对仍保持「面板内恰好一个指向该用户的链接」。
4. 评论输入区既没有 contenteditable、也没有 comment-input-placeholder
   （点一下才激活编辑器）。修：容器内取面积最大的非按钮元素当占位，
   误点不会假成功 —— 后续 EDITOR_JS 校验恰好一个可见 contenteditable。
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v30.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v31.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]

o.cell(row=125, column=7).value = (
    o.cell(row=125, column=7).value
    + "。【真机验收第二轮 2026-11-18 21:00–21:40，双账号】"
      "① 关注 success，账本记 follow_sent，同目标二次执行 skipped/already_sent（幂等生效）；"
      "② 双账号并行 4 条线索 4/4 success，两账号首条相隔 4s、第二批相隔 2s，"
      "总跨度 20.3s（串行至少 60s），同账号两条之间 18s（动作耗时 + 3–8s 随机间隔），"
      "并行与串行节奏均符合预期；"
      "③ 首作点赞 read-author-works → like-video success；"
      "④ 首作评论 post-comment success（话术与用户确认的一致，未被 AI 改写 —— 需显式传 "
      "profileCommentUseAi=false）；"
      "⑤ 私信未通：点「私信」打开的是消息列表（面板文本为 55 条历史会话），"
      "没有进入与目标用户的对话，需在抽屉内搜索并选定用户，属多步交互，本轮未实现。"
      "本轮真机暴露并修掉 4 个壳侧通用缺陷："
      "(a) 驻留窗被 DWM 判遮挡 → visibilityState 恒 hidden → 所有写动作停在 live_preview_not_visible，"
      "改为核验时临时置顶 + 通过后 releaseTopmost（不 hide）并给冷启动留呈现时间；"
      "(b) 主页有两个「私信」按钮（一个是 0×0 隐藏节点）使「全局唯一」判定必失败，改为先按可见尺寸过滤；"
      "(c) 私信面板 class 名漂移到 componentsRightPanel*，改为按右侧抽屉尺寸定位（宽 240–720、高≥200 取最外层）；"
      "(d) 评论输入区无 contenteditable 也无 placeholder（点一下才激活），改为取容器内面积最大的非按钮元素，"
      "误点由 EDITOR_JS 严格校验兜住。"
      "分享主页仍未跑（需提供接收方好友昵称）。"
)

dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 21:45　　真机验收第二轮（v31）："
    "批量跟进的双账号并行（4/4 success，总跨度 20.3s vs 串行 60s+）、关注、首作点赞、首作评论全部在真实抖音页面跑通；"
    "私信未通——点「私信」打开的是消息列表而非与目标用户的会话，属多步交互，本轮未实现。"
    "同时修掉 4 个壳侧通用缺陷：驻留窗被判遮挡导致写动作集体失败（置顶 + 冷启动等待）、"
    "私信按钮存在 0×0 隐藏节点破坏「唯一」判定、私信面板 class 漂移（改按抽屉尺寸定位）、"
    "评论输入区改为点击激活式占位定位。"
    "全量回归 1225 项 = 1222 passed + 3 failed（3 项均为 patchright 未安装，与基线一致）。"
)

wb.save(DST)

chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
note = str(w.cell(row=125, column=7).value or "")
print("#124 状态:", w.cell(row=125, column=6).value)
print("  含并行证据:", "20.3s" in note, "| 含私信未通:", "私信未通" in note)
print("看板含 v31:", "v31" in (chk["统计看板"]["A3"].value or ""))
