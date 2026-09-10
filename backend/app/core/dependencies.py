import os

from app.repositories.base import Repository
from app.repositories.memory import MemoryRepository
from app.repositories.sqlite import SqliteRepository
from app.services.account_service import AccountService
from app.services.business_service import BusinessService
from app.services.ai_service import AIService
from app.services.comment_task_service import CommentTaskService
from app.services.compliance_service import ComplianceService
from app.services.conversation_service import ConversationService
from app.services.crawl_service import CrawlService
from app.services.customer_service import CustomerService
from app.services.dashboard_service import DashboardService
from app.services.dm_service import DmService
from app.services.dm_inbox_service import DmInboxService
from app.services.followup_service import FollowUpService
from app.services.lead_service import LeadService
from app.services.notification_service import NotificationService
from app.services.onboarding_service import OnboardingService
from app.services.script_service import ScriptService
from app.services.settings_service import SettingsService
from app.services.task_service import TaskService
from app.services.workbench_service import WorkbenchService
from app.services.scheduler_service import SchedulerService
from app.services.export_service import ExportService
from app.services.report_service import ReportService
from app.services.backup_service import BackupService
from app.services.monitor_service import MonitorService
from app.services.dm_sender_service import DmSenderService

def _create_repository() -> Repository:
    """根据环境变量 REPO_BACKEND 创建 Repository 实例

    REPO_BACKEND=sqlite (默认) → SqliteRepository
    REPO_BACKEND=memory         → MemoryRepository
    SqliteRepository 初始化失败时自动 fallback 到 MemoryRepository
    """
    backend = os.environ.get("REPO_BACKEND", "sqlite").lower()
    if backend == "memory":
        return MemoryRepository()
    try:
        return SqliteRepository()
    except Exception as e:
        print(f"[dependencies] SqliteRepository 初始化失败，fallback 到 MemoryRepository: {e}")
        return MemoryRepository()


_repo: Repository = _create_repository()
_notification_service = NotificationService(_repo)
_lead_service = LeadService(_repo)
_task_service = TaskService(_repo)
_followup_service = FollowUpService(_repo)
_customer_service = CustomerService(_repo)
_conversation_service = ConversationService(_repo)
_account_service = AccountService(_repo)
_ai_service = AIService()
# 注入 LeadService，用于 AI 画像分析后自动回写 intent_level/tags/customer_need
_ai_service.set_lead_service(_lead_service)
_business_service = BusinessService(_repo)
_script_service = ScriptService(_repo)
_dm_service = DmService(_repo)
_dm_inbox_service = DmInboxService(_repo)
_comment_task_service = CommentTaskService(_repo)
_crawl_service = CrawlService(_repo)
_dashboard_service = DashboardService(_repo)
_compliance_service = ComplianceService(_repo)
_workbench_service = WorkbenchService(_repo)
_onboarding_service = OnboardingService(_repo)
_settings_service = SettingsService(_repo)
# 双向注入：settings_service 热更新 AI 配置，ai_service 从 settings_service 读取配置
_settings_service.set_ai_service(_ai_service)
_ai_service.set_settings_service(_settings_service)
_scheduler_service = SchedulerService(_repo)
_export_service = ExportService(_repo)
_report_service = ReportService(_repo)

# ── 注入通知服务：关键业务事件自动触发通知（P4-A）──
_dm_inbox_service.set_notification_service(_notification_service)
_customer_service.set_notification_service(_notification_service)
_lead_service.set_notification_service(_notification_service)
_account_service.set_notification_service(_notification_service)
_backup_service = BackupService(_repo)
_monitor_service = MonitorService(_repo, _ai_service)
_dm_sender_service = DmSenderService(_repo)


def get_lead_service() -> LeadService:
    return _lead_service


def get_task_service() -> TaskService:
    return _task_service


def get_followup_service() -> FollowUpService:
    return _followup_service


def get_customer_service() -> CustomerService:
    return _customer_service


def get_conversation_service() -> ConversationService:
    return _conversation_service


def get_account_service() -> AccountService:
    return _account_service


def get_ai_service() -> AIService:
    return _ai_service


def get_repository() -> Repository:
    return _repo


def get_business_service() -> BusinessService:
    return _business_service


def get_script_service() -> ScriptService:
    return _script_service


def get_dm_service() -> DmService:
    return _dm_service


def get_dm_inbox_service() -> DmInboxService:
    return _dm_inbox_service


def get_comment_task_service() -> CommentTaskService:
    return _comment_task_service


def get_crawl_service() -> CrawlService:
    return _crawl_service


def get_dashboard_service() -> DashboardService:
    return _dashboard_service


def get_compliance_service() -> ComplianceService:
    return _compliance_service


def get_workbench_service() -> WorkbenchService:
    return _workbench_service


def get_onboarding_service() -> OnboardingService:
    return _onboarding_service


def get_settings_service() -> SettingsService:
    return _settings_service


def get_scheduler_service() -> SchedulerService:
    return _scheduler_service


def get_export_service() -> ExportService:
    return _export_service


def get_report_service() -> ReportService:
    return _report_service

def get_backup_service() -> BackupService:
    return _backup_service


def get_monitor_service() -> MonitorService:
    return _monitor_service

def get_notification_service() -> NotificationService:
    return _notification_service


def get_dm_sender_service() -> DmSenderService:
    return _dm_sender_service
