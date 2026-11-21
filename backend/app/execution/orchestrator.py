"""M1 任务编排器（D22）—— collect → filter → engage → dm → done 全流水线。

在 Pipeline（collect/filter，W1 已完成）之上补齐：

- engage 阶段：对达标候选执行互动动作，动作执行器（engage_fn）注入式接入
  （LikeWorker / CommentWorker 的 run_one 即符合签名）；
- dm 阶段：对 DM 达标线索执行私信，执行器（dm_fn）注入式接入
  （DMChannelRouter.send 包装即可）；执行前必须先过 dm_frequency_guard；
- 断点续跑：engage/dm 阶段逐条目 checkpoint（pipeline_progress 表），
  杀进程重启后从断点继续，已完成条目跳过，配合 worker 幂等键双重防重复；
- 合规闸门（§0.4 第 4 条）：engage_fn / dm_fn 未注入时阶段标记 blocked 并
  跳过，**绝不触发任何真实平台写动作**。真实链路须先过 TEST-CHECKLIST B 组。

接口（app/api/pipeline_api.py）：
    POST /api/pipeline/start
    GET  /api/pipeline/{run_id}/status
    POST /api/pipeline/{run_id}/stop
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from app.execution.pipeline import Pipeline
from app.execution.store import ExecutionStore

EngageFn = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
DmFn = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


class Orchestrator:
    """把 Pipeline 串到 done 的可恢复编排器。"""

    STAGES = Pipeline.STAGES

    def __init__(self, store: ExecutionStore, pipeline: Pipeline) -> None:
        self.store = store
        self.pipeline = pipeline
        # 接管 engage/dm：Pipeline 只跑到 filter 即交棒
        self.pipeline.stop_after = "filter"

    def run(
        self,
        run_id: str,
        video_urls: list[str],
        *,
        engage_fn: EngageFn | None = None,
        dm_fn: DmFn | None = None,
        min_score: int | None = None,
        source_keyword: str = "",
        max_comments_per_video: int = 50,
        resume: bool = True,
        stop_flag: object = None,
    ) -> dict:
        """执行（或续跑）全流水线。返回最终 run 状态（含 stats）。"""
        self.pipeline.run(
            run_id,
            video_urls,
            source_keyword=source_keyword,
            max_comments_per_video=max_comments_per_video,
            resume=resume,
            stop_flag=stop_flag,
        )
        run = self.store.get_run(run_id)
        stage = (run or {}).get("stage", "collect")
        stats = json.loads((run or {}).get("stats") or "{}")

        # 纯 Pipeline 的 done（旧 runs / 幂等重放）且 engage 未处理过 → 继续编排；
        # 已带 engage 统计的 done = 编排已完成的 run，直接幂等返回。
        if stage == "done" and not any(
            k.startswith("engage_") or k.startswith("dm_") for k in stats):
            stage = "filter"

        if stage == "stopped":
            return self._snapshot(run_id)

        if stage in {"collect", "filter", "engage", "dm"}:
            self._engage_stage(run_id, engage_fn, stop_flag)
            run = self.store.get_run(run_id)
            stage = (run or {}).get("stage", "stopped")
        if stage == "stopped":
            return self._snapshot(run_id)

        if stage in {"engage", "dm"}:
            self._dm_stage(run_id, dm_fn, stop_flag)
            run = self.store.get_run(run_id)
            stage = (run or {}).get("stage", "stopped")
        if stage == "stopped":
            return self._snapshot(run_id)

        if stage in {"dm", "done"}:
            run = self.store.get_run(run_id)
            stats = json.loads((run or {}).get("stats") or "{}")
            stats["finished"] = True
            self.store.set_stage(run_id, "done", stats)
        return self._snapshot(run_id)

    # ── 阶段实现 ─────────────────────────────────────────────

    def _engage_stage(self, run_id: str, engage_fn: EngageFn | None,
                      stop_flag: object) -> None:
        if engage_fn is None:
            # 合规闸门：未注入执行器 → blocked，不触发任何平台动作
            stats = self._bump(run_id, engage_blocked="no_handler(合规闸门:未过TEST-CHECKLIST验证)")
            self.store.set_stage(run_id, "dm", stats)
            return
        self._run_items(run_id, "engage", engage_fn, stop_flag,
                        next_stage="dm", key_prefix="cand:")

    def _dm_stage(self, run_id: str, dm_fn: DmFn | None, stop_flag: object) -> None:
        if dm_fn is None:
            stats = self._bump(run_id, dm_blocked="no_handler(合规闸门:未过TEST-CHECKLIST验证)")
            self.store.set_stage(run_id, "done", stats)
            return
        self._run_items(run_id, "dm", dm_fn, stop_flag,
                        next_stage="done", key_prefix="dm:")

    def _run_items(self, run_id: str, stage: str, fn: Callable[..., Any],
                   stop_flag: object, *, next_stage: str, key_prefix: str) -> None:
        stats = self._bump(run_id)
        candidates = self.store.list_candidates(min_score=None)
        done = self.store.done_items(run_id, stage)
        ok = int(stats.get(f"{stage}_ok", 0))
        fail = int(stats.get(f"{stage}_fail", 0))
        uncertain = int(stats.get(f"{stage}_uncertain", 0))
        skipped = int(stats.get(f"{stage}_skipped", 0))

        for cand in candidates:
            if callable(stop_flag) and stop_flag():
                stats.update({f"{stage}_ok": ok, f"{stage}_fail": fail,
                              f"{stage}_uncertain": uncertain, f"{stage}_skipped": skipped})
                self.store.set_stage(run_id, "stopped", stats)
                return
            key = f"{key_prefix}{cand['id']}"
            if key in done:
                skipped += 1
                continue
            try:
                result = fn(cand) or {}
            except Exception as exc:  # 单条失败不阻塞整轮
                fail += 1
                stats[f"{stage}_errors"] = list(stats.get(f"{stage}_errors", [])) + [
                    f"{key}:{type(exc).__name__}"]
                self.store.mark_item_done(run_id, stage, key, result=f"fail:{type(exc).__name__}")
                continue
            status = str(result.get("status", "sent_ok"))
            if status == "sent_ok":
                ok += 1
            elif status == "uncertain":
                uncertain += 1  # 三态机语义：不重试
            else:
                fail += 1
            self.store.mark_item_done(run_id, stage, key, result=status)

        stats.update({f"{stage}_ok": ok, f"{stage}_fail": fail,
                      f"{stage}_uncertain": uncertain, f"{stage}_skipped": skipped})
        self.store.set_stage(run_id, next_stage, stats)

    # ── 工具 ─────────────────────────────────────────────────

    def _bump(self, run_id: str, **extra: object) -> dict:
        run = self.store.get_run(run_id)
        try:
            stats: dict[str, Any] = json.loads((run or {}).get("stats") or "{}")
        except ValueError:
            stats = {}
        stats.update(extra)
        return stats

    def _snapshot(self, run_id: str) -> dict:
        run = self.store.get_run(run_id) or {}
        try:
            stats = json.loads(run.get("stats") or "{}")
        except ValueError:
            stats = {}
        return {"run_id": run_id, "stage": run.get("stage", "unknown"), "stats": stats}
