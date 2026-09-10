from fastapi import APIRouter, Depends

from app.core.dependencies import get_conversation_service
from app.schemas.conversation import ConversationCreate, ConversationRead, ConversationUpdate, DraftCreate, MessageCreate, MessageRead
from app.services.conversation_service import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationRead])
def list_conversations(service: ConversationService = Depends(get_conversation_service)) -> list[ConversationRead]:
    return service.list_conversations()


@router.get("/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: str, service: ConversationService = Depends(get_conversation_service)) -> ConversationRead:
    return service.get_conversation(conversation_id)


@router.post("", response_model=ConversationRead)
def create_conversation(payload: ConversationCreate, service: ConversationService = Depends(get_conversation_service)) -> ConversationRead:
    return service.create_conversation(payload)


@router.patch("/{conversation_id}", response_model=ConversationRead)
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationRead:
    return service.update_conversation(conversation_id, payload)


@router.get("/{conversation_id}/messages", response_model=list[MessageRead])
def list_messages(conversation_id: str, service: ConversationService = Depends(get_conversation_service)) -> list[MessageRead]:
    return service.list_messages(conversation_id)


@router.post("/messages", response_model=MessageRead)
def add_message(payload: MessageCreate, service: ConversationService = Depends(get_conversation_service)) -> MessageRead:
    return service.add_message(payload)


@router.post("/{conversation_id}/drafts", response_model=list[MessageRead])
def generate_ai_drafts(
    conversation_id: str,
    payload: DraftCreate,
    service: ConversationService = Depends(get_conversation_service),
) -> list[MessageRead]:
    return service.generate_ai_drafts(conversation_id, payload)


@router.post("/{conversation_id}/confirm-send", response_model=MessageRead)
def confirm_send(
    conversation_id: str,
    payload: dict,
    service: ConversationService = Depends(get_conversation_service),
) -> MessageRead:
    return service.confirm_send(conversation_id, payload.get("content", ""))
