"""养号配置与自热任务的本地写回。

页面（TaskStore / SelfWarmup）对这两条通道的契约很具体，接错就等于假成功：

    get-nurture-config
        返回值被 Object.assign(d, 返回值) 摊进配置对象。所以**必须返回裸配置**，
        不能包 {"success":true,"config":{}} —— 否则 success / config 两个键会被
        写进用户配置，下次保存再原样存回库，配置里永久多两个假字段。
        没有配置时返回 None，页面会走 localStorage 兜底并把旧配置推上来，
        相当于一次自然的迁移。

    save-nurture-config
        入参就是整份配置（页面 300ms 防抖，每次改动全量提交）。空包不落库：
        初始化阶段先发空包的话，照写会把用户已有配置清空。

    update-self-warmup-task
        入参 {id, patch:{...}}，patch 可能是 autoStartOnLaunch、configSnapshot，
        也可能是「停止任务」的 status/endedAt/endReason。
        页面会校验回执：**返回值里必须有刚写入的那个字段**，否则报「设置未保存」。
        因此这里回执「落库后重新读出来的整条记录」，而不是 {"success":true}。

全部只动本地库，不产生任何平台请求。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.api import task_store
from app import local_store

CATEGORY = "nurture"
CONFIG_KEY = "config"


def load(db_path) -> dict | None:
    """读养号配置：有就返回裸 dict，没有返回 None（让页面走本地兜底）。"""
    data = local_store.read_json(db_path, CONFIG_KEY, CATEGORY)
    return data if isinstance(data, dict) and data else None


def save(db_path, payload: Any) -> dict:
    """整份配置覆盖写入。空包跳过，避免防抖首帧把已有配置刷成空。"""
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, Mapping) or not payload:
        return {"success": True, "skipped": "empty"}
    data = dict(payload)
    try:
        local_store.write_json(db_path, CONFIG_KEY, data, CATEGORY)
    except Exception as exc:  # noqa: BLE001 - sqlite 异常统一转成业务失败
        return {"success": False, "msg": f"养号配置未能写入本地数据库：{exc}"}
    return {"success": True, "keys": len(data)}


def patch_task(db_path, payload: Any) -> Any:
    """按 id 合并 patch 到自热任务，返回落库后的整条记录（供页面校验回执）。

    找不到任务时返回 False：页面两条调用路径都按「假值=失败」处理，
    宁可提示保存失败，也不能回一个空对象让用户以为存上了。
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, Mapping):
        return False
    task_id = str(payload.get("id") or "").strip()
    patch = payload.get("patch")
    if not task_id or not isinstance(patch, Mapping):
        return False
    record = task_store.get_task(db_path, "warmup", task_id)
    if not isinstance(record, dict):
        return False
    merged = dict(record)
    for key, value in patch.items():
        if str(key) == "id":
            continue
        merged[key] = value
    saved = task_store.save_task(db_path, "warmup", merged)
    if not saved.get("success"):
        return False
    # 回执必须是「库里读回来的那条」，而不是拼出来的中间结果：
    # 只有真实落库了，页面下次刷新才能看到同样的值。
    return task_store.get_task(db_path, "warmup", task_id) or False


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
