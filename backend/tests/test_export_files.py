"""导出落盘（save-lead-export-csvs）契约测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app import export_files
from app.paths import EXPORT_DIR


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _post(client, channel, args):
    resp = client.post(f"/ipc/{channel}", json={"args": args})
    assert resp.status_code == 200
    return resp.json()["data"]


def test_export_writes_csv_with_bom(client):
    d = _post(client, "save-lead-export-csvs",
              [{"files": [{"name": "unit_export.csv",
                           "content": "\ufeff昵称,平台\n甲,douyin\n乙,douyin\n"}]}])
    assert d["success"] is True and d["count"] == 1
    path = EXPORT_DIR / "leads" / "unit_export.csv"
    assert path.is_file()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "缺 BOM，Excel 打开会乱码"
    assert len(path.read_text(encoding="utf-8-sig").strip().splitlines()) == 3
    assert d["files"][0]["url"] == "/exports/leads/unit_export.csv"


@pytest.mark.parametrize("raw,expect", [
    ("../../evil.csv", "evil.csv"),
    ("/etc/passwd", "passwd.csv"),
    ("a/b/c.csv", "c.csv"),
    ("noext", "noext.csv"),
])
def test_filename_is_sanitized(raw, expect):
    assert export_files._safe_name(raw) == expect


def test_traversal_lands_inside_export_dir(client):
    d = _post(client, "save-lead-export-csvs",
              [{"files": [{"name": "../../../../escape.csv", "content": "a,b\n1,2\n"}]}])
    assert d["success"] is True
    path = EXPORT_DIR / "leads" / "escape.csv"
    assert path.is_file() and path.resolve().is_relative_to(EXPORT_DIR.resolve())


def test_rejects_empty_and_oversize(client):
    assert _post(client, "save-lead-export-csvs", [{"files": []}])["success"] is False
    assert _post(client, "save-lead-export-csvs",
                 [{"files": [{"name": "x.csv", "content": ""}]}])["success"] is False


def test_multiple_files(client):
    d = _post(client, "save-lead-export-csvs", [{"files": [
        {"name": "part1.csv", "content": "a\n1\n"},
        {"name": "part2.csv", "content": "a\n2\n"},
    ]}])
    assert d["count"] == 2
    assert (EXPORT_DIR / "leads" / "part1.csv").is_file()
    assert (EXPORT_DIR / "leads" / "part2.csv").is_file()
