"""§1.3 合规护栏 —— 最小集字段白名单 / uid 级字段拦截 / CI 静态检查。

三约束（DESIGN-DOC.md §1.3）：
1. 读环节只用浏览器读屏（本模块不负责，见 read_model.py 的数据来源断言）
2. 存储只做筛选最小集，禁 uid 级字段、禁全量评论库
3. 私信官方通道优先（本模块不负责，见 README 交接说明）

设计要点：白名单是唯一放行依据，字段不在白名单里一律拒绝（不是黑名单）。
这样平台改版新增字段时默认被拦住，而不是默认漏进去。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

# lead_candidates 允许落库的字段。与 DESIGN-DOC.md §4 DDL 一一对应。
# comment_text_hash 是去重用派生值（不是用户标识），不持久化原文之外的信息。
CANDIDATE_FIELDS: frozenset[str] = frozenset({
    "id", "video_url", "nickname", "comment_text", "comment_text_hash",
    "intent_score", "intent_tags", "source_keyword", "created_at", "expire_at",
})

# 服务端强制生成的字段：调用方传入即拒绝，防止 TTL 被绕过或哈希被伪造
SERVER_ONLY_FIELDS: frozenset[str] = frozenset({
    "id", "comment_text_hash", "created_at", "expire_at",
})

# 明确点名的禁用标识类字段。命中即拒绝，且报错信息可用于 CI 定位。
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "uid", "sec_uid", "secUid", "webcast_uid", "webcastUid",
    "user_id", "userUrl", "user_url", "authorProfileUrl", "author_profile_url",
    "message_id", "messageId", "lead_id", "leadId", "ip_location", "ipLocation",
    "account_id", "accountId", "privacy_masked", "identity_type",
    "collected_fields", "collectedFields", "raw_page", "raw_html", "cookie",
    "comment_user_id", "comment_id", "video_id", "external_id",
})

# uid 级字段的形态（大小写不敏感、允许下划线/中划线变体）
_UID_PATTERN = re.compile(
    r"(?:^|[_-])(?:uid|secuid|webcastuid|userid|openid|unionid|"
    r"commentuserid|authorid|profileurl|rawpage|rawhtml)(?:$|[_-])",
    re.IGNORECASE,
)


class ComplianceViolation(ValueError):
    """字段超出最小集。调用方必须中止写入，不允许降级放行。"""


def field_violation(field: str) -> str | None:
    """返回违规原因；合规字段返回 None。"""
    normalized = field.strip()
    if normalized in FORBIDDEN_FIELDS:
        return f"禁用标识字段: {normalized}"
    if _UID_PATTERN.search(normalized):
        return f"疑似 uid 级字段: {normalized}"
    if normalized not in CANDIDATE_FIELDS:
        return f"不在最小集白名单: {normalized}"
    return None


def validate_candidate_payload(payload: Mapping[str, object], *, client_side: bool = False) -> None:
    """写入前校验。任何越界字段直接抛 ComplianceViolation。

    client_side=True 用于外部调用入口：额外禁止伪造服务端生成字段
    （id / hash / created_at / expire_at），防止 TTL 被绕过。
    """
    for field in payload:
        if client_side and field in SERVER_ONLY_FIELDS:
            raise ComplianceViolation(f"服务端生成字段不可由调用方传入: {field}")
        reason = field_violation(field)
        if reason is not None:
            raise ComplianceViolation(f"{reason}（payload keys={sorted(payload)}）")


def validate_ddl_columns(columns: Iterable[str]) -> list[str]:
    """校验一张表的列名，返回违规列表（空列表 = 通过）。供 CI 调用。"""
    return [
        f"{col}: {reason}"
        for col in columns
        if (reason := field_violation(col)) is not None
    ]


def scan_source_for_uid_fields(paths: Iterable[str | Path]) -> list[str]:
    """CI 静态检查：扫描新增源码里的 lead_candidates INSERT 是否混入 uid 字段。

    只检查 INSERT INTO lead_candidates 语句的字段列表，不扫描存量业务代码，
    避免把南溪系统历史表（leads 已有 comment_user_id 等列）误报为本次违规。
    """
    insert_pattern = re.compile(
        r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+lead_candidates\s*\(([^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )
    findings: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in insert_pattern.finditer(text):
            column_expr = match.group(1)
            # 动态拼接的列名由运行时 validate_candidate_payload 兜底，静态扫描跳过
            if "{" in column_expr or "}" in column_expr:
                continue
            columns = [c.strip().strip('`"[]') for c in column_expr.split(",")]
            for col in columns:
                if not col:
                    continue
                reason = field_violation(col)
                if reason is not None:
                    findings.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1} {col}: {reason}")
    return findings
