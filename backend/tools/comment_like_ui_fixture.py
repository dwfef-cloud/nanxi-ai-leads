"""Temporary real bridge for Electron UI acceptance; no production data or transport."""
import os
import sys
from pathlib import Path
import sqlite3
import threading

root, port = Path(sys.argv[1]).resolve(), int(sys.argv[2])
root.mkdir(parents=True, exist_ok=True)
os.environ.update(DATA_DIR=str(root), DB_PATH=str(root / "test.db"),
                  LEADS_DB_PATH=str(root / "test.db"), SCHEDULER_OFF="1", ADMIN_ENABLED="0")
os.environ.pop("LEADS_LIVE_ACCEPTANCE_SCOPE", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.init import init_db
init_db()
with sqlite3.connect(root / "test.db") as con:
    con.execute("INSERT INTO accounts(id,nickname,platform,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("fixture-account", "离线测试账号", "douyin", "2026-11-15", "2026-11-15"))
from app.runtime import browser
from app import comment_likes
if os.environ.get("COMMENT_LIKE_UI_LEGACY") == "1":
    # Emulate the still-running older bridge, only in this temporary fixture.
    original_candidates = comment_likes.CommentLikeService.candidates
    def legacy_candidates(self, args):
        result = original_candidates(self, args)
        with self.connect() as con:
            rows = con.execute("SELECT id,url,text,created_ms FROM live_action_results WHERE account_id=?",
                               (str(args.get("accountId") or ""),)).fetchall()
        for item in result.get("items", []):
            matches = [row for row in rows if (row[1], row[2], row[3]) ==
                       (item["videoUrl"], item["content"], item["commentedAt"])]
            if len(matches) == 1:
                item["id"] = f"local:{matches[0][0]}"
        return result
    comment_likes.CommentLikeService.candidates = legacy_candidates
from app.execution.live_leadgen import evidence
for index in range(2):
    evidence(root / "test.db", "fixture-source", action="post-comment", status="sent_ok",
             url="https://www.douyin.com/video/7400000000000000001",
             text=f"离线评论选择验收{index + 1}", account_id="fixture-account")
from app.api import license_local
assert license_local.activate(license_local.issue_demo_code(ttl_hours=1)["code"])["success"]
assert license_local.auth_overlay()["entitlementVerified"]
def no_transport(*args, **kwargs):
    raise AssertionError("UI fixture forbids platform transport")
browser.execute = no_transport
browser.shell_available = lambda: False
from bridge_server import app
if os.environ.get("COMMENT_LIKE_UI_DASHBOARD") == "1":
    # This process owns only the temporary fixture DB. No scope or platform call.
    finish = threading.Event()
    def fixture_plan(self, record, plan):
        assert plan["accounts"] == ["fixture-account"]
        return plan
    def fixture_attempt(self, item, account, target, stop):
        while not finish.wait(.05):
            if stop.is_set():
                return "skipped", "隔离验收：停止已确认，没有平台动作"
        return "uncertain", "隔离验收：刷新未保留，结果待核验"
    comment_likes.CommentLikeService._live_plan = fixture_plan
    comment_likes.CommentLikeService._live_attempt = fixture_attempt
    @app.post("/fixture/finish")
    def finish_fixture():
        finish.set()
        return {"success": True}

@app.middleware("http")
async def log_fixture_channels(request, call_next):
    if request.url.path.startswith("/ipc/"):
        with (root / "channels.log").open("a", encoding="utf-8") as log:
            log.write(request.url.path + "\n")
    return await call_next(request)
import uvicorn
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
def shutdown_on_input():
    for line in sys.stdin:
        if line.strip() == "stop":
            server.should_exit = True
            return
threading.Thread(target=shutdown_on_input, daemon=True).start()
server.run()
