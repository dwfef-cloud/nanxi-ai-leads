"""
数据导出路由 · 线索 / 客户 / 成交记录
========================================
GET /api/export/leads      导出线索（CSV/XLSX）
GET /api/export/customers  导出客户（CSV/XLSX）
GET /api/export/deals      导出成交记录（CSV/XLSX）

返回 JSON 包含 file_url，前端可通过 /exports/ 路径直接下载。
"""
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_export_service
from app.schemas.report import ExportResponse
from app.services.export_service import ExportService

router = APIRouter(tags=["export"])


@router.get("/export/leads", response_model=ExportResponse)
def export_leads(
    format: str = Query("csv", pattern="^(csv|xlsx)$", description="导出格式"),
    status: str | None = Query(None, description="线索状态筛选"),
    source: str | None = Query(None, description="来源筛选"),
    start_date: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    service: ExportService = Depends(get_export_service),
) -> dict:
    """导出线索数据为 CSV/XLSX 文件"""
    return service.export_leads(
        format=format,
        status=status,
        source=source,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/export/customers", response_model=ExportResponse)
def export_customers(
    format: str = Query("csv", pattern="^(csv|xlsx)$", description="导出格式"),
    stage: str | None = Query(None, description="客户阶段筛选"),
    start_date: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    service: ExportService = Depends(get_export_service),
) -> dict:
    """导出客户数据为 CSV/XLSX 文件"""
    return service.export_customers(
        format=format,
        stage=stage,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/export/deals", response_model=ExportResponse)
def export_deals(
    format: str = Query("csv", pattern="^(csv|xlsx)$", description="导出格式"),
    start_date: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    service: ExportService = Depends(get_export_service),
) -> dict:
    """导出成交记录为 CSV/XLSX 文件"""
    return service.export_deals(
        format=format,
        start_date=start_date,
        end_date=end_date,
    )
