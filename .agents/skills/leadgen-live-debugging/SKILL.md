---
name: leadgen-live-debugging
description: Diagnose desktop 评论获客 runtime failures across the UI, Electron IPC, backend execution, task logs, SQLite evidence, and platform acknowledgements; use when a live run stops early or ignores configured parameters.
---

# Leadgen Live Debugging

Use this skill for bugs in the desktop 评论获客 path, especially early termination, ignored settings, failed scrolling/video navigation, missing countdowns, empty comments, and `stream_failed` or model-generation errors. It is project guidance, not authorization to operate a live account.

## Map the execution path

- `ui/` is the desktop UI actually mounted by the backend; `frontend/` is marked legacy in the project README. Confirm which UI/runtime the user launched before comparing behavior.
- Desktop integration and platform-page commands live under `desktop/` (including `main.js`, `live-*.js`, `leadgen-*.js`, and command handlers). Do not infer successful page actions from an IPC request alone; inspect the command acknowledgement and resulting state.
- `/ipc/{channel}` is dispatched by `backend/app/api/ipc_bridge.py`. Runtime task lifecycle and persisted task logs are in `backend/app/api/task_runner.py` and `backend/app/api/task_logs.py`.
- Sequential browsing and per-video recovery live in `backend/app/execution/leadgen_stream.py`; plan construction and filters live in `leadgen_plan.py`; model generation and its safe error conversion live in `live_leadgen.py`; parameter normalization lives in `leadgen_config.py`.
- Runtime service health, task state, and history are distinct evidence. Health does not prove a task is active; a finished task cannot be resumed by restarting the backend.

## Diagnose from safe evidence

1. Confirm the user's exact run by task/run ID and timestamp. Check whether a run is active before inspecting further. Use read-only dashboard/task/log channels when available; never stop, restart, or start a task just to collect diagnostics.
2. Read the task's ordered structured logs and sanitized error metadata. Do not expose or copy comment text, message text, cookies, tokens, or full model request/response bodies into reports or logs. If only a generic exception class is stored, report that the exact cause is not proven.
3. Reconstruct the stage sequence: saved/effective config → desktop command issued → desktop acknowledgement/page state → video/comment read → filter/plan/model stage → write attempt/receipt → persisted task status and counters. Pinpoint the first stage whose expected output is absent.
4. Compare configured targets with the effective config captured for that run. Distinguish requested count, randomized per-run cap, completed videos, skipped candidates, interactions attempted, and confirmed writes; do not treat those counters as interchangeable.
5. Trace the first failing exception to its origin. In particular, distinguish a model-provider transport/auth/config/parse failure (`GenerationError`) from an ordinary `not_suitable` decision, empty-comment skip, video filter, browser navigation error, and uncertain write result.

## Choose recovery behavior

- Retry bounded transient reads/navigation/provider transport failures only when the operation is idempotent and the page/run identity is revalidated. Keep the task active during backoff and log the next action, attempt, and delay.
- Skip the current video and continue only when the failure is isolated to that video and continuing preserves the user's configured source, filters, quotas, account, and target. Record the skip reason and increment the correct counters.
- Treat invalid credentials, invalid model/configuration, exhausted bounded retries, safety-gate failures, and authorization/scope mismatches as task-level errors with a safe actionable message; do not silently retry them per video.
- Never automatically retry a comment/send/write whose result is uncertain. Reconcile by authorized readback or stop to prevent duplicate writes.
- Preserve manual stop/pause semantics. “Keep trying” must not mean unbounded hot loops, changing account/source, bypassing filters, or acting outside the authorized target.

## Change and verify

- Reproduce the failing branch with a focused unit/integration test using mocked desktop/model boundaries; avoid real account actions in tests.
- Add a regression test for both the failure path and the intended continuation/stop behavior, including counters, persisted end reason, and logs when affected.
- Run focused tests first, then the relevant backend suite; run desktop/UI checks when the IPC contract or visible state changes. Report skipped or unavailable tests explicitly.
- Inspect `git diff` before editing and after editing. This repository may contain extensive unrelated user changes; preserve them and keep the patch limited to owned files.
- Restart the desktop/backend only when the user requests it or when loading a verified code change requires it. A restart is not a substitute for root-cause correction and does not resume a finished run.

## Reporting

Give the first failed stage, evidence, confirmed cause versus uncertainty, whether the configured target was reached, and the next safe action. Do not say “fixed” based only on a plausible code change; cite the regression test and runtime verification separately.
