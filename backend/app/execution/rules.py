"""M3 规则层（零成本）—— 噪声过滤 + 关键词词典 + 基础意向分。

设计依据：阿里山 leads-export.json 698 条样本里 541 条是「111」类噪声。
规则层必须先于模型层执行，保证噪声不消耗 LLM 调用、不进入线索池。

保守原则：拿不准就过滤（少召回），不做宽松放行 —— 与 §1.3 最小集一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.execution.params import FILTER

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMOJI_PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,20}\]")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_DIGIT_RE = re.compile(r"\d")
_CJK_OR_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z]+")
_REPEATED_DIGIT_PREFIX = re.compile(r"^\s*(\d)\1{1,}\s*")

# 强意向：明确表达求带/咨询/合作/购买
STRONG_TERMS: tuple[str, ...] = (
    "怎么合作", "怎么加入", "求带", "带带我", "想学", "怎么学", "多少钱", "什么价格",
    "报价", "怎么收费", "私聊", "私信我", "联系我", "加微", "微信多少", "有课程吗",
    "怎么报名", "想了解", "感兴趣", "能教吗", "求资料", "求教程", "求链接",
    "想搭建", "想开发", "需要开发", "帮我搭建", "开发报价", "定制智能体",
)
# 中意向：表达需求或认可，但未明确要联系方式
MEDIUM_TERMS: tuple[str, ...] = (
    "有用", "干货", "收藏", "学到了", "想试试", "怎么做", "求分享",
    "在哪买", "有链接吗", "讲得细", "小白", "新手", "入门", "副业", "兼职", "变现",
)
# 闲聊标签（有内容但无商业意向）。泛夸赞词归此类，不给中意向分，
# 避免「不错/厉害」把纯寒暄抬到接近阈值。
CHAT_TERMS: tuple[str, ...] = (
    "哈哈", "笑死", "沙发", "前排", "打卡", "支持", "加油", "来了", "第一",
    "不错", "厉害", "绝了", "牛",
)

# 标签归类
TAG_TERMS: dict[str, tuple[str, ...]] = {
    "副业": ("副业", "兼职", "变现", "赚钱", "搞钱", "收入"),
    "求带": ("求带", "带带", "想学", "怎么学", "小白", "新手", "入门", "求教程"),
    "咨询": ("多少钱", "报价", "收费", "怎么合作", "想了解", "私聊", "加微", "联系我"),
    "闲聊": CHAT_TERMS,
}


@dataclass(frozen=True)
class RuleVerdict:
    is_noise: bool
    reason: str
    base_score: int
    tags: tuple[str, ...]
    matched_terms: tuple[str, ...]


def effective_text(text: str) -> str:
    """剥离数字、URL、表情占位、标点后剩下的有效自然语言片段。"""
    cleaned = _URL_RE.sub("", text or "")
    cleaned = _EMOJI_PLACEHOLDER_RE.sub("", cleaned)
    return "".join(_CJK_OR_WORD_RE.findall(cleaned))


def _noise_reason(text: str) -> str | None:
    """返回噪声原因；非噪声返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return "empty"

    # 纯 URL / URL 为主体（样本里大量 videoUrl 被塞进 content）
    without_url = _URL_RE.sub("", raw).strip()
    if not without_url:
        return "url_only"
    if len(without_url) < FILTER.min_effective_chars:
        return "after_url_too_short"

    # 表情占位符为主体
    without_emoji = _EMOJI_PLACEHOLDER_RE.sub("", without_url).strip()
    if not without_emoji:
        return "emoji_placeholder"

    # 纯数字 / 纯符号（111、11、1、!!!）
    letters = "".join(_CJK_OR_WORD_RE.findall(without_emoji))
    if not letters:
        return "digits_or_symbols_only"

    # 数字主导 + 有效文字极短：「111已关注」「1111求带」这类刷屏
    digits = len(_DIGIT_RE.findall(without_emoji))
    if digits and digits / max(1, len(without_emoji)) >= 0.5 and len(letters) <= 4:
        return "digit_dominated"

    # 重复数字前缀 + 短尾巴：「111 已三连」（空格会稀释上面的数字占比）
    prefix = _REPEATED_DIGIT_PREFIX.match(without_emoji)
    if prefix and len(letters) <= 6:
        return "digit_prefix_spam"

    # 有效文字过短
    if len(letters) < FILTER.min_effective_chars:
        return "effective_too_short"

    # 单字符无限重复：哈哈哈哈 / aaaa（保留 2-3 字的真实语气词）
    if len(set(letters)) == 1 and len(letters) >= 4:
        return "repeated_single_char"

    # 去标点后为空
    if not _PUNCT_RE.sub("", letters):
        return "punctuation_only"

    return None


def classify(text: str) -> RuleVerdict:
    """对单条评论做规则层判定。"""
    reason = _noise_reason(text)
    if reason is not None:
        return RuleVerdict(True, reason, 0, (), ())

    normalized = (text or "").lower()
    strong = tuple(t for t in STRONG_TERMS if t in normalized)
    medium = tuple(t for t in MEDIUM_TERMS if t in normalized)
    matched = strong + medium

    if strong:
        score = 75
    elif medium:
        score = 55
    else:
        score = 30

    tags = tuple(
        tag for tag, terms in TAG_TERMS.items()
        if any(term in normalized for term in terms)
    )
    if not tags:
        tags = ("其他",)

    return RuleVerdict(False, "ok", score, tags, matched)


def filter_batch(texts: list[str]) -> tuple[list[tuple[str, RuleVerdict]], dict[str, int]]:
    """批量过滤。返回 (保留项, 噪声原因计数)。"""
    kept: list[tuple[str, RuleVerdict]] = []
    noise: dict[str, int] = {}
    for text in texts:
        verdict = classify(text)
        if verdict.is_noise:
            noise[verdict.reason] = noise.get(verdict.reason, 0) + 1
        else:
            kept.append((text, verdict))
    return kept, noise
