# -*- coding: utf-8 -*-
"""从 v31 生成 v32：补登「采集关键词（联想词扩词）」——前端已有、后端整条缺失的功能。

背景（2026-11-18 22:55 用户发现）
----------------------------------
评论获客页的「采集关键词」弹窗（账号选择 / 关键词输入 / 开始采集 / 一键复制 /
导入评论获客 / 去重计数 / 进度条 / 待验证继续）**前端全部已实现**，点击会真实调用：
    keyword-collector-start / -stop / -state / -show / -resume
    （事件订阅 keyword-collector-update）
但后端这 5 个通道**一个都没注册**，全代码 0 处实现 → 点「开始采集」必失败，
结果区永远是空的。也就是说：不是前端没做，是前端在等一个不存在的后端。

本轮补齐（壳 + 后端）
---------------------
1. 壳侧新指令 suggest-keywords：搜索框真实点击 + insertText（脚本 setValue 不触发
   React 联想层），读下拉浮层。取证两轮：
   - 第一轮几何判据选错容器：把顶部导航栏（高 484、21 个子节点）当成联想列表，
     返回「精选/推荐/…」；
   - 第二轮补「必须与输入框左对齐」+ 优先 data-e2e=searchbar-input，命中真实浮层。
   另加 reuse：连查 26 次时复用已加载的首页，省掉每次 3s+ 的重新加载。
2. 后端 app/runtime/keyword_collector.py：状态机（seeds × a..z 展开、跨查询去重保序、
   单实例、软失败记 warning 并推进、撞验证停 verification 等人工继续、停止/恢复）+
   5 个通道注册 + keyword-collector-update 事件推送 + show 调 show-worker。

真机验收（2026-11-18 23:0x，账号 1794886618996「AI学长小马」）
----------------------------------------------------------------
- 关键词「获客」→ 26 组查询全部完成（completed 26 / total 26），status=completed；
- 收集去重后 98 个联想词，warnings 0 条，全程无验证拦截；
- 「查看采集画面」show → visible=true，account 与执行账号一致；
- 进度事件 keyword-collector-update 持续入队（壳已消费），页面进度条可实时刷新。
- 单测 10 项（状态字段契约 / 起始校验 / 单实例 / 去重保序 / 软失败推进 /
  撞验证暂停与继续重跑 / 停止两次状态 / 未知 runId 拒绝）。
- 全量回归 1235 项 = 1232 passed + 3 failed（3 项均为 patchright 未安装，与基线一致）。
"""
from copy import copy

import openpyxl

SRC = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v31.xlsx"
DST = r"C:\Users\Administrator\Desktop\获客智能体-自动回复模块清单_v32.xlsx"

wb = openpyxl.load_workbook(SRC)
o = wb["其他模块"]
last = o.max_row
src_row = last            # 用末行做样式模板

row = last + 1
values = [
    149,
    "评论获客",
    "关键词采集",
    "采集关键词（联想词扩词）",
    "「自动依次查询「关键词 + a」到「关键词 + z」，收集搜索框下拉联想词并去重」；"
    "按钮「开始采集」「一键复制」「导入评论获客」；结果区「已去重 N 个关键词」",
    "已实现",
    "壳 suggest-keywords（真实点击 + insertText 读下拉浮层，reuse 复用首页）；"
    "keyword-collector-start/-stop/-state/-show/-resume 已注册（EXPLICIT → "
    "app/runtime/keyword_collector.py：状态表 keyword_collector_runs + 采集线程）；"
    "进度事件 keyword-collector-update",
    "前端弹窗原本就完整（Settings 预构建产物），此前后端 5 个通道一个都没注册 → "
    "点「开始采集」必失败，属「前端有、后端缺失」。本轮真机：关键词「获客」26/26 组查询完成、"
    "去重 98 词、0 警告；「查看采集画面」visible=true；撞安全验证时停 verification 等人工处理后"
    "「继续采集」（从断点重跑，不跳过）；软失败记 warning 并推进，不会卡死同一条。"
    "取证踩坑：几何判据一开始把顶部导航栏当联想列表，补「与输入框左对齐」+ 优先 "
    "data-e2e=searchbar-input 才命中。单测 10 项。",
]

for col, val in enumerate(values, start=1):
    cell = o.cell(row=row, column=col)
    template = o.cell(row=src_row, column=col)
    cell.value = val
    cell._style = copy(template._style)

dash = wb["统计看板"]
dash["A3"] = dash["A3"].value + "\n" + (
    "统计时间：2026-11-18 23:20　　补登（v32）：评论获客页「采集关键词」（联想词扩词）——"
    "前端弹窗与 5 个通道调用原本就完整，后端一个都没注册，属「前端有、后端缺失」。"
    "本轮补壳侧 suggest-keywords（真实键盘输入读下拉浮层，双向取证：先误抓顶部导航、"
    "补左对齐判据后命中）+ 后端状态机与 5 个通道。真机：关键词「获客」26/26 组完成、"
    "去重 98 词、0 警告，查看采集画面 visible=true。"
    "全量回归 1235 项 = 1232 passed + 3 failed（3 项均为 patchright 未安装，与基线一致）。"
)

wb.save(DST)

chk = openpyxl.load_workbook(DST)
w = chk["其他模块"]
print("新行序号:", w.cell(row=row, column=1).value, "| 状态:", w.cell(row=row, column=6).value)
print("含真机数字:", "98 词" in str(w.cell(row=row, column=8).value),
      "| 含 26/26:", "26/26" in str(w.cell(row=row, column=8).value))
print("看板含 v32:", "v32" in (chk["统计看板"]["A3"].value or ""))
print("行数:", w.max_row, "（v31 为 149）")
