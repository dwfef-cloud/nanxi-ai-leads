"""授权与运营后台（/admin）。

模块边界：
  store.py  数据层（admin_console.db：licenses / instances / usage_daily / audit_log）
  keys.py   密钥签发与校验（HMAC 签名，离线可验）
  auth.py   后台口令与会话
  api.py    /admin/api/* 管理接口 + 客户机接入接口
  web/      后台前端（复用阿里山壳设计 token）

与业务三库完全隔离；交付给客户时可设 ADMIN_ENABLED=0 整体关闭。
"""
from __future__ import annotations

from app.admin.auth import init_credentials
from app.admin.store import AdminStore

__all__ = ["AdminStore", "bootstrap"]


def bootstrap() -> str | None:
    """确保库表与后台口令就绪。返回首次生成的口令明文（仅第一次有值）。"""
    AdminStore()  # 建表
    initial = init_credentials()
    if initial:
        print(
            "\n[admin] 后台已初始化，首次登录口令：" + initial +
            "\n[admin] 已写入 backend/data/admin_initial_password.txt"
            "（登录后请改密并删除该文件）\n"
            "[admin] 入口：http://127.0.0.1:8899/admin/\n",
            flush=True,
        )
    return initial
