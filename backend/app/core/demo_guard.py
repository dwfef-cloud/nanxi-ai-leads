"""演示/假数据接口的开关守卫。

为什么需要
----------
`/demo/seed-report-data` 会一次性写入 42 条「日报客户N」假线索，
`/crawler/import-demo` 会写入硬编码的 demo-user 假线索，两者都直接落主库。
这两个接口是早期为演示日报、跑通导入链路准备的，但**默认敞开在生产路径上**：
一旦被调用（脚本、误点、自检），真库就会混入无法与真线索区分的假数据。

策略
----
默认拒绝。只有显式设置环境变量 `ALLOW_DEMO_SEED=1` 才放行，
供内部演示 / 测试使用；交付包不设该变量，等于永久关闭。
"""
from __future__ import annotations

import os

from fastapi import HTTPException

_ENV_KEY = "ALLOW_DEMO_SEED"
_ALLOWED = {"1", "true", "on", "yes"}


def demo_seed_enabled() -> bool:
    return str(os.getenv(_ENV_KEY, "")).strip().lower() in _ALLOWED


def require_demo_seed() -> None:
    """FastAPI 依赖：未开启时直接 403，不产生任何写入。"""
    if not demo_seed_enabled():
        raise HTTPException(
            status_code=403,
            detail=("演示数据接口已关闭：生产环境不允许向真实线索库灌入假数据。"
                    f"如需内部演示，请设置环境变量 {_ENV_KEY}=1 后重启服务。"),
        )
