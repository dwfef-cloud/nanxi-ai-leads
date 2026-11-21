"""M1 任务编排器 —— collect → filter → engage → dm → done 可恢复状态机。

本轮范围（第 1 周）：只实装 collect / filter 两阶段，产出写 pipeline_runs，
崩溃后从断点续跑。engage / dm 阶段保留状态位但**不接真实写动作**
（§0.4 第 4 条：跑任何互动动作前须先过 TEST-CHECKLIST 验证）。

合规：读屏只取可见文本；落库只走 store 最小集；噪声不调 LLM。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.execution.rules import classify
from app.execution.scorer import IntentScorer, ScoreResult
from app.execution.screen_reader import ScreenReader
from app.execution.store import ExecutionStore


class LeadSink(Protocol):
    """线索池接口。南溪现有 lead_service 后续按此协议接入。"""

    def promote(self, candidate_id: int, score: int) -> None: ...


class NoopLeadSink:
    def promote(self, candidate_id: int, score: int) -> None:
        return None


@dataclass
class PipelineReport:
    run_id: str
    stage: str
    collected: int = 0
    noise_filtered: int = 0
    rejected_by_compliance: int = 0
    duplicates: int = 0
    promoted: int = 0
    degraded: int = 0
    per_video: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "stage": self.stage,
            "collected": self.collected,
            "noise_filtered": self.noise_filtered,
            "rejected_by_compliance": self.rejected_by_compliance,
            "duplicates": self.duplicates,
            "promoted": self.promoted,
            "degraded": self.degraded,
            "per_video": self.per_video,
            "errors": self.errors,
        }


class Pipeline:
    STAGES = ("collect", "filter", "engage", "dm", "done")

    def __init__(
        self,
        store: ExecutionStore,
        reader: ScreenReader,
        scorer: IntentScorer,
        lead_sink: LeadSink | None = None,
        *,
        stop_after: str = "done",
    ) -> None:
        self.store = store
        self.reader = reader
        self.scorer = scorer
        self.lead_sink = lead_sink or NoopLeadSink()
        # D22：编排器接管 engage/dm 时传 "filter"，Pipeline 只跑到 filter 交棒
        self.stop_after = stop_after

    def run(
        self,
        run_id: str,
        video_urls: list[str],
        *,
        source_keyword: str = "",
        max_comments_per_video: int = 50,
        resume: bool = True,
        stop_flag: object = None,
    ) -> PipelineReport:
        """执行（或续跑）一条流水线。stop_flag 为可调用对象时返回 True 即停止。"""
        existing = self.store.get_run(run_id) if resume else None
        start_stage = existing["stage"] if existing and existing["stage"] in self.STAGES else "collect"
        report = PipelineReport(run_id=run_id, stage=start_stage)
        if existing and existing["stats"]:
            try:
                report = _report_from_stats(run_id, start_stage, json.loads(existing["stats"]))
            except (ValueError, TypeError):
                pass

        if not existing:
            self.store.create_run(run_id)

        # D22 断点续跑：collect 阶段逐视频 checkpoint，重启后跳过已采集的视频
        done_videos = self.store.done_items(run_id, "collect") if resume else set()

        if start_stage == "collect":
            for url in video_urls:
                if _is_stopped(stop_flag):
                    self.store.set_stage(run_id, "stopped", report.as_stats())
                    report.stage = "stopped"
                    return report
                if url in done_videos:
                    continue  # 已完成条目，断点跳过
                try:
                    comments = self.reader.read_video_comments(url, max_comments_per_video)
                except Exception as exc:
                    # 单视频失败不影响整轮，记录后继续
                    report.errors.append(f"read_failed:{url}:{type(exc).__name__}")
                    continue
                report.collected += len(comments)
                report.per_video[url] = len(comments)
                for comment in comments:
                    verdict = classify(comment.text)
                    if verdict.is_noise:
                        report.noise_filtered += 1
                        continue
                    result = self.scorer.score(comment.text, verdict)
                    if result.ai_degraded:
                        report.degraded += 1
                    try:
                        candidate_id = self.store.insert_candidate({
                            "video_url": url,
                            "nickname": comment.nickname,
                            "comment_text": comment.text,
                            "source_keyword": source_keyword,
                            "intent_score": result.intent_score,
                            "intent_tags": list(result.intent_tags),
                        })
                    except Exception as exc:
                        report.rejected_by_compliance += 1
                        report.errors.append(f"insert_rejected:{type(exc).__name__}")
                        continue
                    if candidate_id is None:
                        report.duplicates += 1
                        continue
                    if self.scorer.should_promote(result):
                        self.lead_sink.promote(candidate_id, result.intent_score)
                        report.promoted += 1
                # 本视频全部候选处理完毕 → 落 collect 断点
                self.store.mark_item_done(run_id, "collect", url)
            report.stage = "filter"
            self.store.set_stage(run_id, "filter", report.as_stats())
        # engage / dm 未接线，保持 stage 可恢复；不发起任何平台写动作
        if report.stage == "filter":
            if self.stop_after == "filter":
                self.store.set_stage(run_id, "filter", report.as_stats())
                return report
            report.stage = "done"
            self.store.set_stage(run_id, "done", report.as_stats())
        return report


def _is_stopped(stop_flag: object) -> bool:
    return bool(callable(stop_flag) and stop_flag())


def _report_from_stats(run_id: str, stage: str, stats: dict) -> PipelineReport:
    return PipelineReport(
        run_id=run_id,
        stage=stage,
        collected=int(stats.get("collected", 0)),
        noise_filtered=int(stats.get("noise_filtered", 0)),
        rejected_by_compliance=int(stats.get("rejected_by_compliance", 0)),
        duplicates=int(stats.get("duplicates", 0)),
        promoted=int(stats.get("promoted", 0)),
        degraded=int(stats.get("degraded", 0)),
        per_video=dict(stats.get("per_video", {})),
        errors=list(stats.get("errors", [])),
    )
