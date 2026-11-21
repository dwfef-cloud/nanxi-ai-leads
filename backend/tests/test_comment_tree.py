# -*- coding: utf-8 -*-
"""评论树工具：主评 / 楼中楼回复的判定与父子引用。

这些用例锁的是「壳侧字段缺失时不能瞎猜」：depth 缺了宁可当主评，
也不要凭 parentCommentId 以外的东西臆测层级。
"""
from app import comment_tree


def test_depth_drives_kind():
    assert comment_tree.kind_of({"depth": 0}) == "main"
    assert comment_tree.kind_of({"depth": 1}) == "reply"
    assert comment_tree.is_reply({"depth": 2}) is True


def test_missing_depth_is_treated_as_main():
    # 老壳不返回 depth：没有父引用就当主评，不猜
    assert comment_tree.depth_of({}) == 0
    assert comment_tree.kind_of({}) == "main"
    assert comment_tree.depth_of({"depth": "2"}) == 2
    assert comment_tree.depth_of({"depth": True}) == 0      # 布尔不是层级
    assert comment_tree.depth_of({"depth": -3}) == 0


def test_parent_comment_id_counts_as_reply_when_depth_absent():
    # 只有父评论 id 没有 depth：仍然能认出是回复
    assert comment_tree.is_reply({"parentCommentId": "7123456789012345678"}) is True
    assert comment_tree.is_reply({"replyId": "7123456789012345678"}) is True
    assert comment_tree.is_reply({"parentCommentId": "0"}) is False
    # depth 在场时以 depth 为准，不被父引用覆盖
    assert comment_tree.is_reply({"depth": 0, "parentCommentId": "7123456789012345678"}) is False


def test_parent_ref_uses_index_then_field():
    rows = [{"commentId": "111", "depth": 0},
            {"commentId": "222", "depth": 0},
            {"commentId": "333", "depth": 1, "parentIdx": 0},
            {"commentId": "444", "depth": 1, "parentIdx": 99},
            {"commentId": "555", "depth": 1, "parentCommentId": "222"}]
    assert comment_tree.parent_ref(rows[2], rows) == "111"
    # 下标越界（结果被裁过）时回退字段，不能靠越界下标取到错误的人
    assert comment_tree.parent_ref(rows[3], rows) == ""
    assert comment_tree.parent_ref(rows[4], rows) == "222"
    # 父行没带 commentId 也不能崩
    assert comment_tree.parent_ref(rows[2], [{"depth": 0}]) == ""


def test_annotate_keeps_order_and_original_rows():
    rows = [{"commentId": "1", "nickname": "甲", "text": "主评", "depth": 0},
            {"commentId": "2", "nickname": "乙", "text": "回复", "depth": 1, "parentIdx": 0}]
    out = comment_tree.annotate(rows)
    assert [row["commentId"] for row in out] == ["1", "2"]
    assert out[0]["kind"] == "main" and out[0]["parentCommentId"] == ""
    assert out[1]["kind"] == "reply" and out[1]["parentCommentId"] == "1"
    # 不改调用方手里的原对象：同一份结果可能被两个模块共用
    assert "kind" not in rows[0]


def test_mains_and_replies_filter():
    rows = [{"commentId": "1", "depth": 0}, {"commentId": "2", "depth": 1},
            {"commentId": "3", "depth": 0}, "坏数据"]
    assert [r["commentId"] for r in comment_tree.mains(rows)] == ["1", "3"]
    assert [r["commentId"] for r in comment_tree.replies(rows)] == ["2"]


def test_annotate_skips_non_dict_rows():
    assert comment_tree.annotate([None, "x", {"depth": 0}]) == [
        {"depth": 0, "kind": "main", "parentCommentId": ""}]
