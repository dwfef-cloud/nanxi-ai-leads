from pydantic import BaseModel, Field


class AcquisitionStartRequest(BaseModel):
    keyword: str = Field(min_length=1)
    count: int = Field(default=10, ge=1, le=100)
    source_mode: str = Field(default="search")
    note: str = ""
    tag: str = ""
    raw_text: str = ""
    file_path: str = ""
    auto_run: bool = True


class LiveAcquisitionRequest(BaseModel):
    keyword: str = Field(min_length=1)
    intent_keywords: str = ""
    excluded_keywords: str = ""
    count: int = Field(default=10, ge=1, le=100)
    max_comments_count: int = Field(default=50, ge=1, le=1000)
    login_type: str = "qrcode"
    headless: bool = False
