"""API 统计中间件 · 内存计数器（P4-C 系统监控）

记录每个端点的请求数 / 错误数 / 累计响应时间。
- 纯内存存储，进程重启清零（不做持久化）
- 端点 key 为去掉查询参数的请求路径
- 错误判定：状态码 >= 400
- 通过 register_api_stats(app) 在 create_app() 中挂载
- 通过 get_api_stats() 供 MonitorService 读取
"""
from __future__ import annotations

import time
import threading
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

# 进程启动时间（用于 uptime）
_START_TS = time.time()

# 内存统计：{endpoint: {"total": int, "errors": int, "total_time": float}}
_stats: dict[str, dict[str, float]] = {}
_lock = threading.Lock()

# 不纳入统计的路径前缀（静态资源 / 导出文件，避免噪音）
_SKIP_PREFIXES = ("/assets/", "/exports/")


def _record(endpoint: str, status_code: int, elapsed: float) -> None:
    with _lock:
        entry = _stats.get(endpoint)
        if entry is None:
            entry = {"total": 0, "errors": 0, "total_time": 0.0}
            _stats[endpoint] = entry
        entry["total"] += 1
        entry["total_time"] += elapsed
        if status_code >= 400:
            entry["errors"] += 1


def get_api_stats() -> dict[str, Any]:
    """供 MonitorService 调用：汇总 API 统计

    Returns:
        {
            "total_requests": int,
            "total_errors": int,
            "error_rate": float,          # 0-100
            "avg_response_time": float,   # ms
            "uptime": float,              # 秒
            "endpoints": {endpoint: {"total","errors","avg_time","error_rate"}},
            "top_5_endpoints": [ {endpoint,total,errors,error_rate,avg_time} ],
        }
    """
    now = time.time()
    with _lock:
        # 深拷贝，避免外部持有引用
        snap = {ep: dict(v) for ep, v in _stats.items()}

    total_requests = 0
    total_errors = 0
    total_time = 0.0
    endpoints: dict[str, Any] = {}

    for ep, v in snap.items():
        t = int(v["total"])
        e = int(v["errors"])
        tt = float(v["total_time"])
        total_requests += t
        total_errors += e
        total_time += tt
        endpoints[ep] = {
            "total": t,
            "errors": e,
            "avg_time": round(tt / t * 1000, 2) if t else 0.0,  # ms
            "error_rate": round(e / t * 100, 2) if t else 0.0,
        }

    top = sorted(endpoints.items(), key=lambda kv: kv[1]["total"], reverse=True)[:5]
    top_5 = [
        {
            "endpoint": ep,
            "total": v["total"],
            "errors": v["errors"],
            "avg_time": v["avg_time"],
            "error_rate": v["error_rate"],
        }
        for ep, v in top
    ]

    return {
        "total_requests": total_requests,
        "total_errors": total_errors,
        "error_rate": round(total_errors / total_requests * 100, 2) if total_requests else 0.0,
        "avg_response_time": round(total_time / total_requests * 1000, 2) if total_requests else 0.0,
        "uptime": round(now - _START_TS, 1),
        "endpoints": endpoints,
        "top_5_endpoints": top_5,
    }


class ApiStatsMiddleware(BaseHTTPMiddleware):
    """记录每个 API 端点的请求数 / 错误数 / 响应时间"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 跳过静态资源与导出文件
        skip = path.startswith(_SKIP_PREFIXES) or path in ("/",)
        start = time.perf_counter()
        status_code = 200
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            if not skip:
                elapsed = time.perf_counter() - start
                _record(path, status_code, elapsed)


def register_api_stats(app: FastAPI) -> None:
    """在 create_app() 中调用，挂载 API 统计中间件"""
    app.add_middleware(ApiStatsMiddleware)
