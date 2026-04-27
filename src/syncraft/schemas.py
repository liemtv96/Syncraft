from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


StorageEngine = Literal["local", "postgresql", "mysql", "mongodb", "dynamodb"]
Platform = Literal[
    "telegram",
    "slack",
    "discord",
    "mattermost",
    "rocketchat",
]
AuthType = Literal["webhook", "bot", "oauth"]
ChannelStatus = Literal["active", "inactive", "error"]
EventScope = Literal["app", "channel"]
LastTestStatus = Literal["", "untested", "success", "failed"]
AssetType = Literal["document", "image", "video", "audio", "text", "transcript", "recording"]
AssetStatus = Literal["synced", "pending", "failed"]
ActivityStatus = Literal["info", "success", "warning", "error"]


class DbConnectionConfig(BaseModel):
    host: str = ""
    port: str = ""
    database: str = ""
    username: str = ""
    password: str = ""


class SettingsPayload(BaseModel):
    push_confirmations: bool = True
    error_alerts: bool = True
    compact_mode: bool = False
    keyboard_shortcuts: bool = True
    dock_magnification: bool = False
    dock_auto_hide: bool = False
    channel_test_message: str = "Syncraft says hi"
    storage_engine: StorageEngine = "local"
    db_connection: DbConnectionConfig = Field(default_factory=DbConnectionConfig)
    local_db_path: str = ""


class SettingsResponse(SettingsPayload):
    model_config = ConfigDict(from_attributes=True)

    id: int
    current_local_db_path: str = ""
    default_local_db_path: str = ""
    created_at: datetime
    updated_at: datetime


class ChannelCapabilities(BaseModel):
    text: bool = True
    files: bool = False
    images: bool = False
    video: bool = False


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)

    def get_str(self, key: str) -> str:
        value = self.values.get(key, "")
        return value if isinstance(value, str) else ""


class ChannelBase(BaseModel):
    name: str
    platform: Platform
    status: ChannelStatus = "active"
    auth_type: AuthType
    capabilities: ChannelCapabilities = Field(default_factory=ChannelCapabilities)
    auth_config: ChannelConfig = Field(default_factory=ChannelConfig)
    target_config: ChannelConfig = Field(default_factory=ChannelConfig)
    provider_account_label: str = ""
    target_label: str = ""
    last_checked: datetime | None = None
    setup_error: str = ""
    last_test_status: LastTestStatus = ""


class ChannelCreate(ChannelBase):
    pass


class ChannelUpdate(BaseModel):
    name: str | None = None
    status: ChannelStatus | None = None
    auth_config: ChannelConfig | None = None
    target_config: ChannelConfig | None = None
    provider_account_label: str | None = None
    target_label: str | None = None
    last_checked: datetime | None = None
    capabilities: ChannelCapabilities | None = None
    setup_error: str | None = None
    last_test_status: LastTestStatus | None = None


class ChannelResponse(ChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class TemplateBase(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    icon: str = "code"
    language: str | None = None


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    icon: str | None = None
    language: str | None = None


class TemplateResponse(TemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class VariableBase(BaseModel):
    name: str
    default_value: str = ""
    description: str = ""


class VariableCreate(VariableBase):
    pass


class VariableUpdate(BaseModel):
    name: str | None = None
    default_value: str | None = None
    description: str | None = None


class VariableResponse(VariableBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class ApiKeyBase(BaseModel):
    name: str
    provider: str
    description: str = ""
    is_active: bool = True


class ApiKeyCreate(ApiKeyBase):
    secret_value: str = Field(min_length=1)


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    description: str | None = None
    secret_value: str | None = None
    is_active: bool | None = None


class ApiKeyResponse(ApiKeyBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    masked_value: str
    created_at: datetime
    updated_at: datetime


class WebhookBase(BaseModel):
    name: str
    target_url: str
    event_scope: EventScope = "app"
    http_method: str = "POST"
    secret: str = ""
    is_enabled: bool = True
    channel_id: str | None = None


class WebhookCreate(WebhookBase):
    pass


class WebhookUpdate(BaseModel):
    name: str | None = None
    target_url: str | None = None
    event_scope: EventScope | None = None
    http_method: str | None = None
    secret: str | None = None
    is_enabled: bool | None = None
    channel_id: str | None = None


class WebhookResponse(WebhookBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseModel):
    status: str
    environment: str
    database_engine: str


class BroadcastAttachment(BaseModel):
    name: str
    type: str
    size: int
    mime_type: str = "application/octet-stream"
    content_base64: str = ""
    mirror_group_id: str = ""


class BroadcastRequest(BaseModel):
    title: str = ""
    content: str = ""
    channel_ids: list[str] = Field(default_factory=list)
    attachments: list[BroadcastAttachment] = Field(default_factory=list)
    asset_source: str = "push_content"


class AssetBase(BaseModel):
    channel_id: str | None = None
    platform: Platform
    name: str
    type: AssetType
    mime_type: str = "application/octet-stream"
    size_bytes: int
    provider_asset_id: str = ""
    provider_message_id: str = ""
    remote_url: str = ""
    preview_url: str = ""
    mirror_group_id: str = ""
    status: AssetStatus = "synced"
    tags: list[str] = Field(default_factory=list)
    mirrors: int = 0


class AssetCreate(AssetBase):
    pass


class AssetUpdate(BaseModel):
    tags: list[str] | None = None
    status: AssetStatus | None = None
    mirrors: int | None = None


class AssetResponse(AssetBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class AssetMirrorRequest(BaseModel):
    channel_ids: list[str] = Field(default_factory=list)


class AssetMirrorChannelResult(BaseModel):
    channel_id: str
    channel_name: str
    status: str
    message: str


class AssetMirrorResponse(BaseModel):
    status: str
    mirrored_count: int
    skipped_count: int
    results: list[AssetMirrorChannelResult] = Field(default_factory=list)


class ChannelMessageBase(BaseModel):
    channel_id: str | None = None
    platform: Platform
    provider_chat_id: str = ""
    provider_message_id: str = ""
    author_name: str = ""
    content: str = ""
    attachment_count: int = 0
    external_url: str = ""
    sent_at: datetime | None = None


class ChannelMessageCreate(ChannelMessageBase):
    pass


class ChannelMessageResponse(ChannelMessageBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class ActivityLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    action: str
    status: ActivityStatus
    title: str
    detail: str
    entity_type: str
    entity_id: str
    channel_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class BroadcastChannelResult(BaseModel):
    channel_id: str
    channel_name: str
    status: LastTestStatus
    message: str


class BroadcastResponse(BaseModel):
    status: str
    results: list[BroadcastChannelResult]
