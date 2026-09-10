from fastapi import APIRouter

from app.api.routes.accounts import router as accounts_router
from app.api.routes.business import router as business_router
from app.api.routes.workbench import router as workbench_router
from app.api.routes.demo import router as demo_router
from app.api.routes.ai import router as ai_router
from app.api.routes.comments import router as comments_router
from app.api.routes.compliance import router as compliance_router
from app.api.routes.crawl import router as crawl_router
from app.api.routes.crawler import router as crawler_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.dm import router as dm_router
from app.api.routes.dm_inbox import router as dm_inbox_router
from app.api.routes.health import router as health_router
from app.api.routes.overview import router as overview_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.customers import router as customers_router
from app.api.routes.followups import router as followups_router
from app.api.routes.leads import router as leads_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.scripts import router as scripts_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.analytics import router as analytics_router
from app.api.routes.behavior import router as behavior_router
from app.api.routes.outreach import router as outreach_router
from app.api.routes.settings import router as settings_router
from app.api.routes.scheduler import router as scheduler_router
from app.api.routes.export import router as export_router
from app.api.routes.reports import router as reports_router
from app.api.routes.dm_sender import router as dm_sender_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.monitor import router as monitor_router
from app.api.routes.backup import router as backup_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(leads_router)
api_router.include_router(tasks_router)
api_router.include_router(followups_router)
api_router.include_router(customers_router)
api_router.include_router(conversations_router)
api_router.include_router(accounts_router)
api_router.include_router(scripts_router)
api_router.include_router(dm_router)
api_router.include_router(dm_inbox_router)
api_router.include_router(comments_router)
api_router.include_router(ai_router)
api_router.include_router(overview_router)
api_router.include_router(crawler_router)
api_router.include_router(crawl_router)
api_router.include_router(business_router)
api_router.include_router(workbench_router)
api_router.include_router(dashboard_router)
api_router.include_router(compliance_router)
api_router.include_router(onboarding_router)
api_router.include_router(analytics_router)
api_router.include_router(behavior_router)
api_router.include_router(outreach_router)
api_router.include_router(demo_router)
api_router.include_router(settings_router)
api_router.include_router(scheduler_router)
api_router.include_router(export_router)
api_router.include_router(reports_router)
api_router.include_router(notifications_router)
api_router.include_router(monitor_router)
api_router.include_router(backup_router)
api_router.include_router(dm_sender_router)
