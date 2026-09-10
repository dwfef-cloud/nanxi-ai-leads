"""
ExportService · 数据导出服务
==============================
将线索 / 客户 / 成交数据导出为 CSV 或 XLSX 文件，保存到 D:\\.exports\\。

- CSV 使用 Python 标准库 csv 模块
- XLSX 优先使用 openpyxl，不可用时降级为 CSV 并在 format 字段注明
- 文件名格式：{type}_{YYYYMMDD}_{HHMMSS}.{ext}
- 从 Repository 层查询数据，日期过滤在 Python 层完成
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from app.repositories.base import Repository

# 导出文件根目录（D 盘，C 盘空间不足）
EXPORT_DIR = Path(r"D:\.exports")

# 线索导出列定义（中文表头 + 字段取值函数）
_LEAD_COLUMNS: list[tuple[str, str]] = [
    ("昵称", "nickname"),
    ("平台", "platform"),
    ("来源", "source"),
    ("来源关键词", "source_keyword"),
    ("来源视频", "video"),
    ("评论内容", "comment"),
    ("意向等级", "intent_level"),
    ("意向度", "intent"),
    ("评分", "score"),
    ("标签", "tags"),
    ("状态", "status"),
    ("地区", "region"),
    ("客户需求", "customer_need"),
    ("备注", "note"),
    ("加微时间", "wechat_added_at"),
    ("成交金额", "deal_amount"),
    ("成交时间", "deal_at"),
    ("创建时间", "created_at"),
]

# 客户导出列定义
_CUSTOMER_COLUMNS: list[tuple[str, str]] = [
    ("客户名称", "name"),
    ("来源", "source"),
    ("阶段", "stage"),
    ("关联线索ID", "lead_id"),
    ("预估价值", "est_value"),
    ("成交金额", "deal_amount"),
    ("成交时间", "deal_at"),
    ("加微时间", "wechat_added_at"),
    ("转介绍人", "referrer"),
    ("下一步动作", "next_action"),
    ("下一步时间", "next_at"),
    ("流失原因", "lost_reason"),
    ("创建时间", "created_at"),
]

# 成交记录导出列定义（从 leads 中筛选 deal_won）
_DEAL_COLUMNS: list[tuple[str, str]] = [
    ("客户昵称", "nickname"),
    ("平台", "platform"),
    ("来源", "source"),
    ("成交金额", "deal_amount"),
    ("成交时间", "deal_at"),
    ("意向等级", "intent_level"),
    ("地区", "region"),
    ("备注", "note"),
    ("创建时间", "created_at"),
]


def _ensure_export_dir() -> None:
    """确保导出目录存在"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _gen_filename(prefix: str, ext: str) -> str:
    """生成文件名：{prefix}_{YYYYMMDD}_{HHMMSS}.{ext}"""
    now = datetime.now()
    return f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}.{ext}"


def _in_date_range(dt_str: str | None, start_date: str | None, end_date: str | None) -> bool:
    """判断 ISO 日期时间字符串是否在日期区间内（按日期部分比较）"""
    if not dt_str:
        return False
    date_part = dt_str[:10]
    if start_date and date_part < start_date:
        return False
    if end_date and date_part > end_date:
        return False
    return True


def _serialize_value(value) -> str:
    """将字段值序列化为字符串（处理 list / datetime / None）"""
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _try_openpyxl_available() -> bool:
    """检测 openpyxl 是否可用"""
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        return False


class ExportService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        _ensure_export_dir()

    # ═══════════════════════════════════════════════════════
    # 导出线索
    # ═══════════════════════════════════════════════════════

    def export_leads(
        self,
        format: str = "csv",
        status: str | None = None,
        source: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """导出线索数据"""
        leads = self._repo.list_leads(status=status, source=source)
        # 日期过滤（按 created_at）
        if start_date or end_date:
            leads = [
                l for l in leads
                if _in_date_range(l.created_at.isoformat() if l.created_at else None, start_date, end_date)
            ]
        rows = [self._lead_to_row(l) for l in leads]
        return self._write_file("leads", _LEAD_COLUMNS, rows, format)

    @staticmethod
    def _lead_to_row(lead) -> list[str]:
        return [_serialize_value(getattr(lead, field, "")) for _, field in _LEAD_COLUMNS]

    # ═══════════════════════════════════════════════════════
    # 导出客户
    # ═══════════════════════════════════════════════════════

    def export_customers(
        self,
        format: str = "csv",
        stage: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """导出客户数据"""
        customers = self._repo.list_customers(stage=stage)
        if start_date or end_date:
            customers = [
                c for c in customers
                if _in_date_range(c.created_at.isoformat() if c.created_at else None, start_date, end_date)
            ]
        rows = [self._customer_to_row(c) for c in customers]
        return self._write_file("customers", _CUSTOMER_COLUMNS, rows, format)

    @staticmethod
    def _customer_to_row(customer) -> list[str]:
        return [_serialize_value(getattr(customer, field, "")) for _, field in _CUSTOMER_COLUMNS]

    # ═══════════════════════════════════════════════════════
    # 导出成交记录
    # ═══════════════════════════════════════════════════════

    def export_deals(
        self,
        format: str = "csv",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """导出成交记录（从线索中筛选 status=deal_won）"""
        leads = self._repo.list_leads()
        deals = [l for l in leads if l.status == "deal_won"]
        if start_date or end_date:
            deals = [
                d for d in deals
                if _in_date_range(d.deal_at.isoformat() if d.deal_at else None, start_date, end_date)
            ]
        rows = [self._deal_to_row(d) for d in deals]
        return self._write_file("deals", _DEAL_COLUMNS, rows, format)

    @staticmethod
    def _deal_to_row(lead) -> list[str]:
        return [_serialize_value(getattr(lead, field, "")) for _, field in _DEAL_COLUMNS]

    # ═══════════════════════════════════════════════════════
    # 文件写入核心
    # ═══════════════════════════════════════════════════════

    def _write_file(
        self,
        prefix: str,
        columns: list[tuple[str, str]],
        rows: list[list[str]],
        fmt: str,
    ) -> dict:
        """将表头+数据写入文件，返回导出信息"""
        _ensure_export_dir()
        use_xlsx = fmt.lower() == "xlsx" and _try_openpyxl_available()
        actual_format = "xlsx" if use_xlsx else "csv"
        ext = "xlsx" if use_xlsx else "csv"
        file_name = _gen_filename(prefix, ext)
        file_path = EXPORT_DIR / file_name

        headers = [col[0] for col in columns]

        if use_xlsx:
            self._write_xlsx(file_path, headers, rows)
        else:
            self._write_csv(file_path, headers, rows)

        return {
            "file_url": f"/exports/{file_name}",
            "file_name": file_name,
            "row_count": len(rows),
            "format": actual_format,
        }

    @staticmethod
    def _write_csv(file_path: Path, headers: list[str], rows: list[list[str]]) -> None:
        """写入 CSV（utf-8-sig 编码，Excel 可直接打开中文）"""
        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)

    @staticmethod
    def _write_xlsx(file_path: Path, headers: list[str], rows: list[list[str]]) -> None:
        """写入 XLSX（openpyxl）"""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "数据导出"
        ws.append(headers)
        for row in rows:
            ws.append(row)
        # 自动列宽（简单估算）
        for col_idx, header in enumerate(headers, 1):
            max_len = max(len(str(header)), max((len(str(r[col_idx - 1])) for r in rows), default=0))
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = min(max_len + 4, 40)
        wb.save(str(file_path))
