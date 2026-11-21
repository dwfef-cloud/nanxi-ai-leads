"""壳指令队列：长指令执行期间不能被判定为「执行器掉线」。

真机现象（2026-11-14）：养号一条 warmup-watch 执行 44 秒（打开详情页 + 停留 + 点赞），
壳在这期间不轮询，30 秒存活窗口一过，后端就判 engine=none，
后面连续 6 轮全部报「桌面壳未加载采集执行器」，直到这条指令跑完才恢复。
"""
from __future__ import annotations

import time

from app.api import desktop_commands as dc


def setup_function():
    dc.reset_for_tests()


def teardown_function():
    dc.reset_for_tests()


def test_inflight_command_keeps_agent_alive():
    cmd_id = dc.publish({"type": "warmup-watch", "scrolls": 2})
    # 模拟：壳 60 秒没轮询（正在执行这条指令），已超出 30 秒存活窗口
    dc._LAST_POLL = time.time() - 60  # noqa: SLF001 测试直接摆状态

    assert dc.agent_alive() is True, "指令还在执行，不能判掉线"
    assert dc.stats()["inflight"] == 1

    dc.submit_result(cmd_id, {"ok": True})
    assert dc.agent_alive() is False, "结果回来后，超窗就该判掉线"
    assert dc.stats()["inflight"] == 0


def test_wait_timeout_clears_inflight():
    cmd_id = dc.publish({"type": "warmup-watch"})
    assert dc.wait_result(cmd_id, timeout=0.1) is None
    assert dc.stats()["inflight"] == 0, "超时要清掉在飞标记，否则永远 alive"


def test_stale_inflight_is_dropped():
    dc.publish({"type": "warmup-watch"})
    # 伪造一条 10 分钟前的在飞指令（壳崩溃没回传）
    with dc._LOCK:  # noqa: SLF001
        for key in list(dc._INFLIGHT):  # noqa: SLF001
            dc._INFLIGHT[key] = time.time() - 600  # noqa: SLF001
    dc._LAST_POLL = time.time() - 60  # noqa: SLF001
    assert dc.agent_alive() is False, "超过最长容忍时间的在飞指令要作废"


def test_long_configured_watch_stays_alive_but_expires_after_its_own_deadline():
    cmd_id = dc.publish({"type": "watch-video", "seconds": 3600})
    dc._LAST_POLL = time.time() - 60
    with dc._LOCK:
        dc._INFLIGHT[cmd_id] = time.time() - 1800
    assert dc.agent_alive()
    with dc._LOCK:
        dc._INFLIGHT[cmd_id] = time.time() - 3800
    assert not dc.agent_alive()
    assert cmd_id not in dc._INFLIGHT_TIMEOUTS


def test_stop_cancels_taken_command_and_ignores_late_success():
    cmd_id = dc.publish({"type": "warmup-watch", "accountId": "a"})
    assert dc.next_command("shell")["id"] == cmd_id
    assert dc.wait_result(cmd_id, stop_check=lambda: True) is None
    assert dc.command_status(cmd_id) == {"cancelled": True, "reason": "manual_stop"}
    dc.submit_result(cmd_id, {"ok": True, "actions": [{"round": 1}]})
    assert dc.stats()["results"] == 0
    assert dc.stats()["inflight"] == 0


def test_timeout_is_delivered_to_desktop_without_cancelling_other_command():
    first = dc.publish({"type": "warmup-watch", "accountId": "a"})
    other = dc.publish({"type": "warmup-watch", "accountId": "b"})
    assert dc.wait_result(first, timeout=0.01) is None
    assert dc.command_status(first) == {"cancelled": True, "reason": "timeout"}
    assert dc.command_status(other)["cancelled"] is False
    assert [c["id"] for c in dc.peek_queue()] == [other]


def test_video_progress_cannot_update_another_or_stopped_command():
    cid = dc.publish({"type": "warmup-watch", "accountId": "account-a"})
    assert dc.warmup_progress_account(cid) == ""
    dc.next_command()
    assert dc.warmup_progress_account(cid) == "account-a"
    assert dc.warmup_progress_account("another") == ""
    dc.wait_result(cid, stop_check=lambda: True)
    assert dc.warmup_progress_account(cid) == ""


# --------------------------------------------------------------------------
# 并发派发（2026-11-18）：同账号串行、跨账号并行、shell-task 独占
# 背景：壳侧执行循环改成并发槽位后，队列必须按账号过滤，否则同一账号的
# 两条指令会在两个窗口里同时跑（同一个 Cookie 却被两处操作）。
# --------------------------------------------------------------------------
def test_same_account_commands_are_serialized():
    first = dc.publish({"type": "read-comments", "accountId": "a"})
    second = dc.publish({"type": "read-comments", "accountId": "a"})

    assert dc.next_command("shell").get("id") == first
    assert dc.next_command("shell") is None, "同账号上一条还在跑，第二条不能派发"

    dc.submit_result(first, {"ok": True})
    assert dc.next_command("shell").get("id") == second


def test_different_accounts_run_in_parallel():
    a = dc.publish({"type": "read-comments", "accountId": "a"})
    b = dc.publish({"type": "read-comments", "accountId": "b"})
    got = {dc.next_command("shell")["id"], dc.next_command("shell")["id"]}
    assert got == {a, b}
    assert dc.stats()["running"] == 2


def test_shell_task_waits_until_nothing_is_running():
    busy = dc.publish({"type": "read-comments", "accountId": "a"})
    shell = dc.publish({"type": "shell-task", "op": "clear-cache"})

    assert dc.next_command("shell")["id"] == busy
    assert dc.next_command("shell") is None, "系统任务独占：还有账号指令在跑就不能派发"

    dc.submit_result(busy, {"ok": True})
    assert dc.next_command("shell")["id"] == shell


def test_account_command_waits_while_shell_task_runs():
    shell = dc.publish({"type": "shell-task", "op": "clear-cache"})
    other = dc.publish({"type": "read-comments", "accountId": "a"})

    assert dc.next_command("shell")["id"] == shell
    assert dc.next_command("shell") is None, "系统任务在跑时，账号指令也不能插进来"

    dc.submit_result(shell, {"ok": True})
    assert dc.next_command("shell")["id"] == other


def test_requeue_puts_command_back_without_losing_it():
    cid = dc.publish({"type": "read-comments", "accountId": "a"})
    assert dc.next_command("shell")["id"] == cid
    assert dc.requeue_command(cid) is True
    assert dc.stats()["running"] == 0
    assert dc.next_command("shell")["id"] == cid, "回队后必须能重新派发"
    assert dc.requeue_command("not-taken") is False


def test_stale_running_command_is_reaped():
    cid = dc.publish({"type": "read-comments", "accountId": "a"})
    assert dc.next_command("shell")["id"] == cid
    with dc._LOCK:  # noqa: SLF001 伪造壳崩溃：占位很久没回传
        dc._RUNNING_AT[cid] = time.time() - 3600  # noqa: SLF001
    assert dc.stats()["running"] == 0
    again = dc.publish({"type": "read-comments", "accountId": "a"})
    assert dc.next_command("shell")["id"] == again, "超时占位要回收，否则该账号永远派不出指令"
