from datetime import datetime, timezone

from app.models.domain import Conversation, Message, now_utc
from app.repositories.memory import MemoryRepository
from app.schemas.conversation import ConversationCreate, ConversationUpdate, DraftCreate, MessageCreate
from app.services.ai_service import AIService


class ConversationService:
    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo
        self._ai_service = AIService()

    def list_conversations(self) -> list[Conversation]:
        return self._repo.list_conversations()

    def get_conversation(self, conversation_id: str) -> Conversation:
        return self._repo.get_conversation(conversation_id)

    def create_conversation(self, payload: ConversationCreate) -> Conversation:
        conversation = Conversation(lead_id=payload.lead_id, account_name=payload.account_name)
        return self._repo.save_conversation(conversation)

    def update_conversation(self, conversation_id: str, payload: ConversationUpdate) -> Conversation:
        conversation = self._repo.get_conversation(conversation_id)
        if payload.status is not None:
            conversation.status = payload.status  # type: ignore[assignment]
        conversation.updated_at = now_utc()
        return self._repo.save_conversation(conversation)

    def list_messages(self, conversation_id: str | None = None) -> list[Message]:
        return self._repo.list_messages(conversation_id)

    def add_message(self, payload: MessageCreate) -> Message:
        conversation = self._repo.get_conversation(payload.conversation_id)
        conversation.last_message_at = now_utc()
        conversation.updated_at = conversation.last_message_at
        self._repo.save_conversation(conversation)
        message = Message(
            conversation_id=payload.conversation_id,
            sender=payload.sender,  # type: ignore[arg-type]
            content=payload.content,
            is_ai_generated=payload.is_ai_generated,
        )
        return self._repo.save_message(message)

    def generate_ai_drafts(self, conversation_id: str, payload: DraftCreate) -> list[Message]:
        drafts = self._ai_service.draft_reply(payload.lead_nickname, payload.source_keyword, payload.user_note)
        stored: list[Message] = []
        for draft in drafts:
            stored.append(
                self.add_message(
                    MessageCreate(
                        conversation_id=conversation_id,
                        sender="ai",
                        content=draft.content,
                        is_ai_generated=True,
                    )
                )
            )
        return stored

    def confirm_send(self, conversation_id: str, content: str) -> Message:
        return self.add_message(
            MessageCreate(
                conversation_id=conversation_id,
                sender="human",
                content=content,
                is_ai_generated=False,
            )
        )
