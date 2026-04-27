from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from syncraft.database import Base


def _uuid() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class AppSettings(Base, TimestampMixin):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    push_confirmations: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_alerts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    compact_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    keyboard_shortcuts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dock_magnification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dock_auto_hide: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    channel_test_message: Mapped[str] = mapped_column(Text, default="Syncraft says hi", nullable=False)
    storage_engine: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    db_host: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    db_port: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    db_database: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    db_username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    db_password: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    local_db_directory: Mapped[str] = mapped_column(String(1024), default="", nullable=False)


class Channel(Base, TimestampMixin):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)
    credentials: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False)
    channel_identifier: Mapped[str | None] = mapped_column(String(255))
    auth_config_json: Mapped[str | None] = mapped_column(Text)
    target_config_json: Mapped[str | None] = mapped_column(Text)
    provider_account_label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    target_label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    setup_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_test_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime)
    asset_sync_cursor: Mapped[str] = mapped_column(Text, default="", nullable=False)
    asset_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    message_sync_cursor: Mapped[str] = mapped_column(Text, default="", nullable=False)
    message_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    webhooks: Mapped[list["Webhook"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Template(Base, TimestampMixin):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(32), default="code", nullable=False)
    language: Mapped[str | None] = mapped_column(String(64))


class Variable(Base, TimestampMixin):
    __tablename__ = "variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    default_value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_asset_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    remote_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    preview_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mirror_group_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="synced", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    mirrors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    channel: Mapped[Channel | None] = relationship()


class ChannelMessage(Base, TimestampMixin):
    __tablename__ = "channel_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_chat_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    author_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    channel: Mapped[Channel | None] = relationship()


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    channel: Mapped[Channel | None] = relationship()


class Webhook(Base, TimestampMixin):
    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    event_scope: Mapped[str] = mapped_column(String(32), default="app", nullable=False)
    http_method: Mapped[str] = mapped_column(String(16), default="POST", nullable=False)
    secret: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))

    channel: Mapped[Channel | None] = relationship(back_populates="webhooks")
