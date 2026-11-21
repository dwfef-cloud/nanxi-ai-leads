from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest

from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.execution.offline_simulation import OfflineSimulator, SimulatedAction
from app.execution.write_safety import WriteResult, WriteStatus


VIDEO = "https://www.douyin.com/video/7400000000000000001"


def test_pending_reservation_survives_restart(tmp_path):
    path = tmp_path / "offline.db"
    gate = PersistentWriteSafetyGate(path)
    assert gate.reserve("post:a:v")[0]
    restarted = PersistentWriteSafetyGate(path)
    assert restarted.reserve("post:a:v") == (False, "uncertain_pending_manual")


def test_confirmed_action_does_not_repeat_after_restart_or_url_variation(tmp_path):
    path = tmp_path / "offline.db"
    first = OfflineSimulator(lambda _: {"http_status": 200, "body": {
        "status_code": 0, "comment": {"cid": "c1"}}}, gate=PersistentWriteSafetyGate(path))
    assert first.run(SimulatedAction("post_comment", "account", VIDEO, "这是离线生成的内容")) == "simulated_ok"
    def never_send(_):
        raise AssertionError("duplicate reached driver")
    second = OfflineSimulator(never_send, gate=PersistentWriteSafetyGate(path))
    assert second.run(SimulatedAction("post_comment", "account", VIDEO + "?from=search#detail",
                                      "更换文案也不能重复发表")) == "skipped_already_sent"
    assert second.counts["post_comment"] == 0


def test_atomic_reservation_across_independent_connections(tmp_path):
    path = tmp_path / "offline.db"
    gates = [PersistentWriteSafetyGate(path) for _ in range(8)]
    barrier = Barrier(len(gates))
    def reserve(gate):
        barrier.wait()
        return gate.reserve("same-target")[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, gates)) == 1


def test_manual_confirmation_releases_only_uncertain(tmp_path):
    gate = PersistentWriteSafetyGate(tmp_path / "offline.db")
    gate.reserve("key")
    gate.confirm_manual("key", sent=False)
    assert gate.reserve("key")[0]
    gate.record("key", WriteResult(WriteStatus.SENT_OK, False, False, "fixture"))
    gate.confirm_manual("key", sent=False)
    assert gate.reserve("key") == (False, "already_sent")


def test_offline_and_live_namespaces_cannot_share_confirmation(tmp_path):
    path = tmp_path / "isolated.db"
    offline = PersistentWriteSafetyGate(path)
    live = PersistentWriteSafetyGate(path, namespace="live")
    offline.reserve("key")
    offline.record("key", WriteResult(WriteStatus.SENT_OK, False, False, "fixture"))
    assert live.can_attempt("key") == (True, "allowed")
    with pytest.raises(ValueError, match="live ledger"):
        OfflineSimulator(lambda _: None, gate=live)


def test_different_accounts_and_actions_remain_independent(tmp_path):
    gate = PersistentWriteSafetyGate(tmp_path / "offline.db")
    assert gate.reserve("comment:a:v")[0]
    assert gate.reserve("comment:b:v")[0]
    assert gate.reserve("favorite:a:v")[0]


def test_late_result_cannot_overwrite_a_newer_reservation(tmp_path):
    gate = PersistentWriteSafetyGate(tmp_path / "offline.db")
    gate.reserve("key")
    old_token = gate.reservation_token("key")
    gate.confirm_manual("key", sent=False)
    gate.reserve("key")
    with pytest.raises(RuntimeError, match="pending reservation"):
        gate.record("key", WriteResult(WriteStatus.SENT_OK, False, False, "late"), token=old_token)
    assert gate.state("key") == "uncertain"
