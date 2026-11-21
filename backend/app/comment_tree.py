# -*- coding: utf-8 -*-
"""评论树：把壳侧返回的扁平评论行还原成「主评 / 回复」。

壳侧 `COMMENT_EXTRACT_JS`（desktop/douyin_agent.js）给每条评论补了四个字段：

    depth       DOM 嵌套层数，0 = 主评，≥1 = 楼中楼回复
    parentIdx   父评论在本次结果数组里的下标（不是评论 id）
    parentNick  父评论昵称
    isAuthor    昵称旁有「作者」徽章 —— **指视频作者，不是「我」**。
                在自己作品下读时它等价于「这条是我发的」，在别人的视频下
                读时它是「up 主发的」，两者不能混用。

2026-11-18 真机实证两件事，写在这里免得后面又踩：

1. 抖音二级评论在 DOM 上是**扁平一层**：同一楼里所有回复都直接挂在一级评论下，
   所以「我回 A、B 回我」两条回复的 depth 都是 1、parentIdx 都指向同一条一级评论。
   depth 只能分出「主评 / 回复」两层，判不出「这条回复是回谁的」。
2. 真正的父子关系要靠平台评论接口的 `reply_id`，但二级评论接口需要签名参数，
   页面内直接 fetch 会超时（实测 14s 拿不到），所以目前拿不到。

需要「谁回复了我」这种精确关系时，用 autoreply 里「同一楼在我最后一次发言之后
出现的新回复」这条判据，不要指望 depth。
"""

from __future__ import annotations


def depth_of(row: dict) -> int:
    """DOM 嵌套层数；字段缺失或不是数字时按主评（0）处理。"""
    if not isinstance(row, dict):
        return 0
    value = row.get("depth")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def is_reply(row: dict) -> bool:
    """是不是楼中楼回复（depth ≥ 1）。

    depth 缺失时回退行上的 parentCommentId / replyId —— 老版本壳不返回 depth，
    但某些链路会带父评论 id；只靠 depth 会把那种行误判成主评。
    """
    if not isinstance(row, dict):
        return False
    if row.get("depth") is not None:
        return depth_of(row) > 0
    for key in ("parentCommentId", "replyId"):
        ref = str(row.get(key) or "").strip()
        if ref and ref != "0":
            return True
    return False


def kind_of(row: dict) -> str:
    """样本类型：main / reply。评论点赞的 kind 字段、线索的展示都用这一套。"""
    return "reply" if is_reply(row) else "main"


def is_video_author(row: dict) -> bool:
    """昵称旁有「作者」徽章 —— 视频作者，不一定是当前登录账号。"""
    return row.get("isAuthor") is True


def parent_ref(row: dict, rows: list[dict]) -> str:
    """父评论的 commentId。

    壳侧给的是 parentIdx（父行在结果数组里的下标），先按它取；父行本身没带
    commentId（例如结果被裁过）时回退行上的 parentCommentId / replyId 字段，
    都没有就返回空串 —— 调用方必须能接受空值。
    """
    idx = row.get("parentIdx")
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(rows):
        ref = str(rows[idx].get("commentId") or "").strip()
        if ref:
            return ref
    for key in ("parentCommentId", "replyId"):
        ref = str(row.get(key) or "").strip()
        if ref and ref != "0":
            return ref
    return ""


def annotate(rows: list[dict]) -> list[dict]:
    """给每行补 kind / parentCommentId，保持原顺序（父在子前）。

    返回新列表，不改调用方手里的原对象 —— 同一份结果可能被两个模块共用。
    """
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 先用原始行判 kind：如果先塞一个 depth=0 进去，is_reply 的
        # parentCommentId 回退分支就永远走不到了（踩过）。
        reply = is_reply(row)
        out.append({**row,
                    "depth": depth_of(row) if row.get("depth") is not None else (1 if reply else 0),
                    "kind": "reply" if reply else "main",
                    "parentCommentId": parent_ref(row, rows)})
    return out


def mains(rows: list[dict]) -> list[dict]:
    """只留主评。吞评检测这类「按主评样本核对」的场景必须用它，
    否则别人回复里出现同样一句话，会被当成「我的主评还在」。"""
    return [row for row in rows if isinstance(row, dict) and not is_reply(row)]


def replies(rows: list[dict]) -> list[dict]:
    """只留楼中楼回复。"""
    return [row for row in rows if isinstance(row, dict) and is_reply(row)]
