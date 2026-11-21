"""南溪 AI 获客 · 执行层（DESIGN-DOC.md M1-M8 第 1 周实现）

合规边界：本包只实现「读屏可见文本 → 规则/模型筛选 → 最小集落库」，
不含任何签名构造、接口拦截、平台写动作（点赞/评论/私信/养号）。
"""
from app.execution.compliance import ComplianceViolation
from app.execution.pipeline import Pipeline
from app.execution.rules import classify
from app.execution.scorer import IntentScorer
from app.execution.screen_reader import ScreenReader
from app.execution.store import ExecutionStore
from app.execution.write_safety import WriteSafetyGate, classify_response

__all__ = [
    "ComplianceViolation",
    "ExecutionStore",
    "IntentScorer",
    "Pipeline",
    "ScreenReader",
    "WriteSafetyGate",
    "classify",
    "classify_response",
]
