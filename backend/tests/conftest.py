"""测试全局隔离（必须在任何测试模块导入前生效）。

历史问题：16 个契约测试文件通过 TestClient(app.main) 读写数据库，
而 app.main 侧仓库走 app/db/init.py → DB_PATH 环境变量，缺省落在
backend/data/lead_system.db —— 每次跑全量测试都会往真实库里写
sales-1 测试账号、限流预警通知等垃圾数据（账号池里的重复账号即来源于此）。

本 conftest 把 app.main 侧的 DB_PATH / 备份 / 导出目录指到临时目录；
桥接侧（ipc_bridge → app/paths.py → LEADS_DB_PATH）不受影响，
依赖真实数据的只读断言（如 test_ipc_bridge）照常读真库。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="nanxi-tests-"))

# app/db/init.py 在每次 get_db_path() 时动态读取，指向临时库
os.environ["DB_PATH"] = str(_TMP / "app_main.db")
# paths.py 在导入时读取，重定向备份 / 导出，避免测试运行产生真实备份文件
os.environ.setdefault("BACKUP_DIR", str(_TMP / "backups"))
os.environ.setdefault("EXPORT_DIR", str(_TMP / "exports"))

# 桥接侧同样隔离：复制一份真库到临时目录，读写全在副本上。
# 这样「依赖真实数据」的只读断言照样成立，但任何写操作都落不到真库。
# 真库位置 = 软件根/data（v1.9.4 起数据已移出 backend/）。
_REAL_DB = _BACKEND.parent / "data" / "lead_system.db"
_COPY_DB = _TMP / "lead_system.db"

# DATA_DIR 无条件指向临时目录：即使真库复制失败，其余库（admin_console /
# account_pool / execution / bridge_license）也绝不会写到真实数据目录。
os.environ["DATA_DIR"] = str(_TMP)
if _REAL_DB.is_file():
    try:
        shutil.copy2(_REAL_DB, _COPY_DB)
        os.environ["LEADS_DB_PATH"] = str(_COPY_DB)
    except OSError:
        pass  # 真库被占用时退回临时空库（依赖真数据的断言会失败，但不会污染）

# 演示数据接口默认关闭（app/core/demo_guard.py）：测试需要时显式开启
os.environ.setdefault("ALLOW_DEMO_SEED", "1")

# 定时排期执行器在测试里不自动起线程：契约测试会写 09:00-18:00 的排期，
# 常驻 tick 会在测试过程中真的去启动任务，制造不可预期的并发与写库。
# 需要验证执行行为时直接调 scheduler.run_once(db, now_ms)，时间可控。
os.environ["SCHEDULER_OFF"] = "1"
