"""
MemoryRepository · 内存版数据访问实现
======================================
开发/测试用，重启丢失。实现 Repository 接口。
生产环境替换为 SqliteRepository，业务层零改动。
"""
from __future__ import annotations

from collections.abc import Iterable

from app.models.domain import (
    Account, AudienceProfile, BusinessProfile, CommentReplyTask,
    ComplianceEvent, ComplianceRule, Conversation, CrawlTask, Customer, CustomerLog,
    DmSendResult, FollowUp, Lead, LeadBehaviorEvent, Message, Notification, ProductKnowledge,
    RuntimeState, Script, ScriptStrategy, ScriptTemplate, ScriptVariant, Task,
    WeChatSettings,
)
from app.repositories.base import Repository


class MemoryRepository(Repository):
    def __init__(self) -> None:
        # ── 获客域 ──
        self._leads: dict[str, Lead] = {}
        self._lead_index: dict[tuple[str, str], str] = {}
        self._accounts: dict[str, Account] = {}
        self._scripts: dict[str, Script] = {}
        self._variants: dict[str, ScriptVariant] = {}
        self._script_templates: list[ScriptTemplate] = [
            ScriptTemplate(industry="装修", count=8, desc="首次触达×3 / 报价跟进×2 / 加微引导×3", installed=True),
            ScriptTemplate(industry="教育", count=0, desc="v2 规划中", installed=False),
            ScriptTemplate(industry="医疗口腔", count=0, desc="v2 规划中", installed=False),
        ]
        self._comment_tasks: dict[str, CommentReplyTask] = {}
        # ── 客户域 ──
        self._customers: dict[str, Customer] = {}
        self._customer_logs: dict[str, list[CustomerLog]] = {}
        self._followups: dict[str, FollowUp] = {}
        # ── 消息域 ──
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, Message] = {}
        # ── 合规域 ──
        self._compliance_events: list[ComplianceEvent] = []
        self._compliance_rules: dict[str, ComplianceRule] = {
            "R1": ComplianceRule(rule_id="R1", desc="单账号日频 ≥ 80 → 降速至 40", status="standby"),
            "R2": ComplianceRule(rule_id="R2", desc="话术变体加微转化率 < 8%（样本≥30）→ 自动切换", status="standby"),
            "R3": ComplianceRule(rule_id="R3", desc="黑名单日增 > 3% 或账号封禁 → 全量暂停", status="standby"),
        }
        self._runtime = RuntimeState()
        # ── 任务域 ──
        self._tasks: dict[str, Task] = {}
        self._behavior_events: dict[str, LeadBehaviorEvent] = {}
        # ── 采集域 ──
        self._crawl_tasks: dict[str, CrawlTask] = {}
        # ── 配置域 ──
        self._business_profile = BusinessProfile()
        self._product_knowledge = ProductKnowledge()
        self._audience_profile = AudienceProfile()
        self._script_strategy = ScriptStrategy()
        self._wechat_settings = WeChatSettings()
        # ── 系统配置中心 ──
        self._system_settings: dict[str, dict[str, str]] = {}
        # ── 通知域（P4-A）──
        self._notifications: dict[str, Notification] = {}
        # ── 私信发送执行域（P4-D）──
        self._dm_send_results: list[DmSendResult] = []

    # ═══════════════════════════════════════════════════════
    # Lead
    # ═══════════════════════════════════════════════════════

    def list_leads(
        self,
        status: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
        min_score: int | None = None,
    ) -> list[Lead]:
        leads = list(self._leads.values())
        if status and status != "all":
            leads = [l for l in leads if l.status == status]
        if source:
            leads = [l for l in leads if l.source == source]
        if min_score is not None:
            leads = [l for l in leads if l.score >= min_score]
        if keyword:
            needle = keyword.lower()
            leads = [
                l for l in leads
                if needle in l.nickname.lower()
                or needle in l.comment.lower()
                or needle in (l.note or "").lower()
                or any(needle in t.lower() for t in l.tags)
            ]
        return leads

    def get_lead(self, lead_id: str) -> Lead:
        return self._leads[lead_id]

    def save_lead(self, lead: Lead) -> Lead:
        self._leads[lead.id] = lead
        if lead.external_id:
            self._lead_index[(lead.platform, lead.external_id)] = lead.id
        return lead

    def find_lead_by_external(self, platform: str, external_id: str) -> Lead | None:
        lead_id = self._lead_index.get((platform, external_id))
        return self._leads.get(lead_id) if lead_id else None

    def delete_lead(self, lead_id: str) -> None:
        lead = self._leads.pop(lead_id, None)
        if lead and lead.external_id:
            self._lead_index.pop((lead.platform, lead.external_id), None)

    def extend_leads(self, leads: Iterable[Lead]) -> None:
        for lead in leads:
            self.save_lead(lead)

    # ═══════════════════════════════════════════════════════
    # Account
    # ═══════════════════════════════════════════════════════

    def list_accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def get_account(self, account_id: str) -> Account:
        return self._accounts[account_id]

    def save_account(self, account: Account) -> Account:
        self._accounts[account.id] = account
        return account

    # ═══════════════════════════════════════════════════════
    # Script / Variant / Template
    # ═══════════════════════════════════════════════════════

    def list_scripts(self, category: str | None = None, active: bool | None = None) -> list[Script]:
        scripts = list(self._scripts.values())
        if category:
            scripts = [s for s in scripts if s.category == category]
        if active is not None:
            scripts = [s for s in scripts if s.active == active]
        return scripts

    def get_script(self, script_id: str) -> Script:
        return self._scripts[script_id]

    def save_script(self, script: Script) -> Script:
        self._scripts[script.id] = script
        return script

    def list_variants(self, script_id: str) -> list[ScriptVariant]:
        return [v for v in self._variants.values() if v.script_id == script_id]

    def save_variant(self, variant: ScriptVariant) -> ScriptVariant:
        self._variants[variant.id] = variant
        return variant

    def list_script_templates(self) -> list[ScriptTemplate]:
        return list(self._script_templates)

    # ═══════════════════════════════════════════════════════
    # CommentReplyTask
    # ═══════════════════════════════════════════════════════

    def list_comment_tasks(self, status: str | None = None) -> list[CommentReplyTask]:
        tasks = list(self._comment_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_comment_task(self, task_id: str) -> CommentReplyTask:
        return self._comment_tasks[task_id]

    def save_comment_task(self, task: CommentReplyTask) -> CommentReplyTask:
        self._comment_tasks[task.id] = task
        return task

    # ═══════════════════════════════════════════════════════
    # Customer / Log
    # ═══════════════════════════════════════════════════════

    def list_customers(self, stage: str | None = None) -> list[Customer]:
        customers = list(self._customers.values())
        if stage:
            customers = [c for c in customers if c.stage == stage]
        return customers

    def get_customer(self, customer_id: str) -> Customer:
        return self._customers[customer_id]

    def save_customer(self, customer: Customer) -> Customer:
        self._customers[customer.id] = customer
        if customer.id not in self._customer_logs:
            self._customer_logs[customer.id] = []
        return customer

    def list_customer_logs(self, customer_id: str) -> list[CustomerLog]:
        return list(self._customer_logs.get(customer_id, []))

    def save_customer_log(self, log: CustomerLog) -> CustomerLog:
        self._customer_logs.setdefault(log.customer_id, []).append(log)
        return log

    # ═══════════════════════════════════════════════════════
    # FollowUp
    # ═══════════════════════════════════════════════════════

    def list_followups(self, date: str | None = None, done: bool | None = None) -> list[FollowUp]:
        items = list(self._followups.values())
        if date == "overdue":
            items = [f for f in items if f.overdue and not f.done]
        elif date == "today":
            items = [f for f in items if not f.overdue and not f.done and "今天" in (f.due or "")]
        elif date == "upcoming":
            items = [f for f in items if not f.done and "今天" not in (f.due or "") and not f.overdue]
        if done is not None:
            items = [f for f in items if f.done == done]
        return items

    def get_followup(self, followup_id: str) -> FollowUp:
        return self._followups[followup_id]

    def save_followup(self, followup: FollowUp) -> FollowUp:
        self._followups[followup.id] = followup
        return followup

    # ═══════════════════════════════════════════════════════
    # Conversation / Message
    # ═══════════════════════════════════════════════════════

    def list_conversations(self) -> list[Conversation]:
        return list(self._conversations.values())

    def get_conversation(self, conversation_id: str) -> Conversation:
        return self._conversations[conversation_id]

    def save_conversation(self, conversation: Conversation) -> Conversation:
        self._conversations[conversation.id] = conversation
        return conversation

    def list_messages(self, conversation_id: str | None = None) -> list[Message]:
        messages = list(self._messages.values())
        if conversation_id:
            messages = [m for m in messages if m.conversation_id == conversation_id]
        return messages

    def save_message(self, message: Message) -> Message:
        self._messages[message.id] = message
        return message

    # ═══════════════════════════════════════════════════════
    # Compliance / Runtime
    # ═══════════════════════════════════════════════════════

    def list_compliance_events(self, limit: int | None = None) -> list[ComplianceEvent]:
        events = list(self._compliance_events)
        return events[:limit] if limit else events

    def save_compliance_event(self, event: ComplianceEvent) -> ComplianceEvent:
        self._compliance_events.insert(0, event)
        return event

    def list_compliance_rules(self) -> list[ComplianceRule]:
        return list(self._compliance_rules.values())

    def save_compliance_rule(self, rule: ComplianceRule) -> ComplianceRule:
        self._compliance_rules[rule.rule_id] = rule
        return rule

    def get_runtime(self) -> RuntimeState:
        return self._runtime

    def save_runtime(self, state: RuntimeState) -> RuntimeState:
        self._runtime = state
        return state

    # ═══════════════════════════════════════════════════════
    # Task / Behavior
    # ═══════════════════════════════════════════════════════

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> Task:
        return self._tasks[task_id]

    def save_task(self, task: Task) -> Task:
        self._tasks[task.id] = task
        return task

    def save_tasks_bulk(self, tasks: Iterable[Task]) -> None:
        for task in tasks:
            self.save_task(task)

    def list_behavior_events(self, lead_id: str | None = None) -> list[LeadBehaviorEvent]:
        events = list(self._behavior_events.values())
        return [e for e in events if e.lead_id == lead_id] if lead_id else events

    def save_behavior_event(self, event: LeadBehaviorEvent) -> LeadBehaviorEvent:
        self._behavior_events[event.id] = event
        return event

    # ═══════════════════════════════════════════════════════
    # 采集域 · CrawlTask
    # ═══════════════════════════════════════════════════════

    def list_crawl_tasks(self, status: str | None = None) -> list[CrawlTask]:
        tasks = list(self._crawl_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get_crawl_task(self, task_id: str) -> CrawlTask:
        return self._crawl_tasks[task_id]

    def save_crawl_task(self, task: CrawlTask) -> CrawlTask:
        self._crawl_tasks[task.id] = task
        return task

    def delete_crawl_task(self, task_id: str) -> None:
        self._crawl_tasks.pop(task_id, None)

    # ═══════════════════════════════════════════════════════
    # 配置域
    # ═══════════════════════════════════════════════════════

    def get_business_profile(self) -> BusinessProfile:
        return self._business_profile

    def save_business_profile(self, profile: BusinessProfile) -> BusinessProfile:
        self._business_profile = profile
        return profile

    def get_product_knowledge(self) -> ProductKnowledge:
        return self._product_knowledge

    def save_product_knowledge(self, value: ProductKnowledge) -> ProductKnowledge:
        self._product_knowledge = value
        return value

    def get_audience_profile(self) -> AudienceProfile:
        return self._audience_profile

    def save_audience_profile(self, value: AudienceProfile) -> AudienceProfile:
        self._audience_profile = value
        return value

    def get_script_strategy(self) -> ScriptStrategy:
        return self._script_strategy

    def save_script_strategy(self, value: ScriptStrategy) -> ScriptStrategy:
        self._script_strategy = value
        return value

    def get_wechat_settings(self) -> WeChatSettings:
        return self._wechat_settings

    def save_wechat_settings(self, value: WeChatSettings) -> WeChatSettings:
        self._wechat_settings = value
        return value

    # ═══════════════════════════════════════════════════════
    # 系统配置中心 · key-value
    # ═══════════════════════════════════════════════════════

    def get_system_settings(self, category: str) -> dict[str, str]:
        return dict(self._system_settings.get(category, {}))

    def set_system_setting(self, category: str, key: str, value: str) -> None:
        if category not in self._system_settings:
            self._system_settings[category] = {}
        self._system_settings[category][key] = value

    def get_all_system_settings(self) -> dict[str, dict[str, str]]:
        return {cat: dict(kv) for cat, kv in self._system_settings.items()}

    # ═══════════════════════════════════════════════════════
    # 聚合查询
    # ═══════════════════════════════════════════════════════

    def summary(self) -> dict[str, int]:
        return {
            "leads": len(self._leads),
            "tasks": len(self._tasks),
            "followups": len(self._followups),
            "conversations": len(self._conversations),
            "messages": len(self._messages),
            "accounts": len(self._accounts),
            "customers": len(self._customers),
            "scripts": len(self._scripts),
        }

    def funnel_stats(self, window_days: int = 30) -> dict:
        leads = list(self._leads.values())
        return {
            "window": f"近 {window_days} 天",
            "stages": [
                {"key": "exposure", "label": "评论区曝光", "value": 0},
                {"key": "collected", "label": "线索入库", "value": len(leads)},
                {"key": "outreach", "label": "私信触达", "value": len([l for l in leads if l.status in ("sent", "replied", "wechat_added", "deal_won")])},
                {"key": "replied", "label": "私信回复", "value": len([l for l in leads if l.status in ("replied", "wechat_added", "deal_won")])},
                {"key": "wechat", "label": "加企微 ★", "value": len([l for l in leads if l.status in ("wechat_added", "deal_won")])},
                {"key": "deal", "label": "成交", "value": len([l for l in leads if l.status == "deal_won"])},
            ],
            "cost": {"totalSpend": 0, "perLead": 0, "perWechatAdd": 0, "perDeal": 0, "avgDealAmount": 0},
            "wechatTrend": [],
        }

    def attribution_stats(self, window_days: int = 30) -> dict:
        from collections import Counter
        leads = list(self._leads.values())
        by_source = []
        for source_key in ["own_comment", "competitor", "inbound_dm", "fan_dm", "lead_card", "referral"]:
            source_leads = [l for l in leads if l.source == source_key]
            by_source.append({
                "source": source_key,
                "label": source_key,
                "leads": len(source_leads),
                "wechat": len([l for l in source_leads if l.status in ("wechat_added", "deal_won")]),
                "deal": len([l for l in source_leads if l.status == "deal_won"]),
                "pilot": source_key in ("lead_card", "referral"),
            })
        return {"window": f"近 {window_days} 天", "bySource": by_source, "topVideos": []}

    def compliance_summary(self) -> dict:
        return {
            "summary": {
                "unsubscribe7d": 0,
                "blacklistTotal": len([l for l in self._leads.values() if l.status == "rejected"]),
                "blacklistDelta7d": 0,
                "complaints7d": 0,
                "auditEvents": len(self._compliance_events),
            },
            "rules": self.list_compliance_rules(),
            "audit": self.list_compliance_events(limit=20),
        }

    # ═══════════════════════════════════════════════════════
    # 调度域
    # ═══════════════════════════════════════════════════════

    def get_scheduler_config(self) -> dict:
        return self._scheduler_config.copy()

    def save_scheduler_config(self, data: dict) -> dict:
        self._scheduler_config.update(data)
        return self._scheduler_config.copy()

    def increment_account_send(self, account_id: str, success: bool) -> None:
        for acc in self._accounts:
            if acc.id == account_id:
                acc.today_sent += 1
                if success:
                    acc.today_success += 1
                break


    # ═══════════════════════════════════════════════════════
    # 通知域 · Notification（P4-A）
    # ═══════════════════════════════════════════════════════

    def list_notifications(self, unread_only: bool = False, limit: int = 50, offset: int = 0) -> list[Notification]:
        items = list(self._notifications.values())
        items.sort(key=lambda n: n.created_at, reverse=True)
        if unread_only:
            items = [n for n in items if not n.read]
        return items[offset: offset + limit]

    def get_unread_count(self) -> int:
        return sum(1 for n in self._notifications.values() if not n.read)

    def mark_notification_read(self, notification_id: str) -> None:
        n = self._notifications.get(notification_id)
        if n is not None:
            n.read = True

    def mark_all_read(self) -> None:
        for n in self._notifications.values():
            n.read = True

    def create_notification(self, notification: Notification) -> Notification:
        self._notifications[notification.id] = notification
        return notification

    # ═══════════════════════════════════════════════════════
    # 私信发送执行域 · DmSendResult（P4-D）
    # ═══════════════════════════════════════════════════════

    def create_dm_send_result(self, result: DmSendResult) -> DmSendResult:
        self._dm_send_results.append(result)
        return result

    def list_dm_send_results(self, limit: int = 50) -> list[DmSendResult]:
        items = sorted(self._dm_send_results, key=lambda r: r.created_at, reverse=True)
        return items[:int(limit)]
