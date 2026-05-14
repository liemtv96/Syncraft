from __future__ import annotations

import base64
import json
import os
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from uuid import uuid4
from urllib import error, parse, request

from syncraft.errors import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from syncraft import models, schemas
from syncraft.config import AppConfig
from syncraft.provider_registry import get_provider_setup

SUPPORTED_PLATFORMS = set(get_args(schemas.Platform))
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_UPLOAD_TIMEOUT_SECONDS = 900.0
UPLOAD_TIMEOUT_BODY_THRESHOLD_BYTES = 10 * 1024 * 1024


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


PROVIDER_TIMEOUT_SECONDS = _env_float(
    "SYNCRAFT_PROVIDER_TIMEOUT_SECONDS",
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
)
PROVIDER_UPLOAD_TIMEOUT_SECONDS = _env_float(
    "SYNCRAFT_PROVIDER_UPLOAD_TIMEOUT_SECONDS",
    DEFAULT_PROVIDER_UPLOAD_TIMEOUT_SECONDS,
)


class ProviderConnectivityError(RuntimeError):
    pass


def _provider_ssl_context(url: str) -> ssl.SSLContext | None:
    if not url.lower().startswith("https://"):
        return None
    context = ssl.create_default_context()
    ignore_unexpected_eof = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if ignore_unexpected_eof:
        context.options |= ignore_unexpected_eof
    return context


def _provider_connectivity_message(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    if isinstance(reason, ssl.SSLEOFError):
        return (
            "provider closed the TLS connection before the upload completed. "
            "This usually means the provider, reverse proxy, or workspace file-size limit rejected the upload."
        )
    return str(reason)


def _mask_secret(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


class StorageService:
    def __init__(self, session: Session):
        self.session = session

    def get_settings(self) -> models.AppSettings:
        settings = self.session.get(models.AppSettings, 1)
        if settings is None:
            now = datetime.utcnow()
            return models.AppSettings(
                id=1,
                channel_test_message="Syncraft says hi",
                created_at=now,
                updated_at=now,
            )
        if settings.channel_test_message != "Syncraft says hi":
            settings.channel_test_message = "Syncraft says hi"
        return settings

    def list_activity_logs(self, limit: int = 200) -> list[models.ActivityLog]:
        return (
            self.session.query(models.ActivityLog)
            .order_by(models.ActivityLog.created_at.desc())
            .limit(limit)
            .all()
        )

    def clear_activity_logs(self) -> None:
        self.session.query(models.ActivityLog).delete()
        self.session.commit()

    def log_activity(
        self,
        *,
        category: str,
        action: str,
        title: str,
        detail: str = "",
        status: str = "info",
        entity_type: str = "",
        entity_id: str = "",
        channel_id: str | None = None,
        metadata: dict[str, object] | None = None,
        commit: bool = True,
    ) -> models.ActivityLog:
        activity = models.ActivityLog(
            category=category,
            action=action,
            status=status,
            title=title,
            detail=detail,
            entity_type=entity_type,
            entity_id=entity_id,
            channel_id=channel_id,
            metadata_json=json.dumps(metadata or {}),
        )
        self.session.add(activity)
        if commit:
            self.session.commit()
            self.session.refresh(activity)
        return activity

    def update_settings(self, payload: schemas.SettingsPayload) -> models.AppSettings:
        settings = self.get_settings()
        settings.push_confirmations = payload.push_confirmations
        settings.error_alerts = payload.error_alerts
        settings.compact_mode = payload.compact_mode
        settings.keyboard_shortcuts = payload.keyboard_shortcuts
        settings.dock_magnification = payload.dock_magnification
        settings.dock_auto_hide = payload.dock_auto_hide
        settings.channel_test_message = payload.channel_test_message
        settings.storage_engine = payload.storage_engine
        settings.db_host = payload.db_connection.host
        settings.db_port = payload.db_connection.port
        settings.db_database = payload.db_connection.database
        settings.db_username = payload.db_connection.username
        settings.db_password = payload.db_connection.password
        settings.local_db_directory = payload.local_db_path.strip()
        self.session.add(settings)
        self.session.commit()
        self.session.refresh(settings)
        self.log_activity(
            category="settings",
            action="settings_updated",
            status="success",
            title="Settings updated",
            detail=f"Storage engine set to {settings.storage_engine}; channel test message updated",
            entity_type="settings",
            entity_id=str(settings.id),
            metadata={
                "storage_engine": settings.storage_engine,
                "push_confirmations": settings.push_confirmations,
                "error_alerts": settings.error_alerts,
                "compact_mode": settings.compact_mode,
            },
        )
        return settings

    def list_channels(self) -> list[models.Channel]:
        channels = self.session.query(models.Channel).order_by(models.Channel.created_at.desc()).all()
        return [channel for channel in channels if channel.platform in SUPPORTED_PLATFORMS]

    def list_templates(self) -> list[models.Template]:
        return self.session.query(models.Template).order_by(models.Template.created_at.desc()).all()

    def create_template(self, payload: schemas.TemplateCreate) -> models.Template:
        template = models.Template(
            name=payload.name,
            description=payload.description,
            content=payload.content,
            icon=payload.icon,
            language=payload.language,
        )
        self.session.add(template)
        self.session.commit()
        self.session.refresh(template)
        self.log_activity(
            category="templates",
            action="template_created",
            status="success",
            title=f"Template created: {template.name}",
            detail="Saved reusable template",
            entity_type="template",
            entity_id=template.id,
            metadata={"icon": template.icon, "language": template.language or ""},
        )
        return template

    def get_template(self, template_id: str) -> models.Template:
        template = self.session.get(models.Template, template_id)
        if template is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
        return template

    def update_template(self, template_id: str, payload: schemas.TemplateUpdate) -> models.Template:
        template = self.get_template(template_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        for key, value in updates.items():
            setattr(template, key, value)
        self.session.add(template)
        self.session.commit()
        self.session.refresh(template)
        self.log_activity(
            category="templates",
            action="template_updated",
            status="success",
            title=f"Template updated: {template.name}",
            detail="Updated reusable template",
            entity_type="template",
            entity_id=template.id,
            metadata={"icon": template.icon, "language": template.language or ""},
        )
        return template

    def delete_template(self, template_id: str) -> None:
        template = self.get_template(template_id)
        template_name = template.name
        self.session.delete(template)
        self.session.commit()
        self.log_activity(
            category="templates",
            action="template_deleted",
            status="warning",
            title=f"Template removed: {template_name}",
            detail="Deleted reusable template",
            entity_type="template",
            entity_id=template_id,
        )

    def list_variables(self) -> list[models.Variable]:
        return self.session.query(models.Variable).order_by(models.Variable.created_at.desc()).all()

    def create_variable(self, payload: schemas.VariableCreate) -> models.Variable:
        existing = (
            self.session.query(models.Variable)
            .filter(models.Variable.name == payload.name)
            .one_or_none()
        )
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Variable '{payload.name}' already exists")
        variable = models.Variable(
            name=payload.name,
            default_value=payload.default_value,
            description=payload.description,
        )
        self.session.add(variable)
        self.session.commit()
        self.session.refresh(variable)
        self.log_activity(
            category="variables",
            action="variable_created",
            status="success",
            title=f"Variable created: {variable.name}",
            detail="Saved reusable variable",
            entity_type="variable",
            entity_id=variable.id,
        )
        return variable

    def get_variable(self, variable_id: str) -> models.Variable:
        variable = self.session.get(models.Variable, variable_id)
        if variable is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variable not found")
        return variable

    def update_variable(self, variable_id: str, payload: schemas.VariableUpdate) -> models.Variable:
        variable = self.get_variable(variable_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        next_name = updates.get("name")
        if next_name is not None and next_name != variable.name:
            existing = (
                self.session.query(models.Variable)
                .filter(models.Variable.name == next_name, models.Variable.id != variable_id)
                .one_or_none()
            )
            if existing is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Variable '{next_name}' already exists")
        for key, value in updates.items():
            setattr(variable, key, value)
        self.session.add(variable)
        self.session.commit()
        self.session.refresh(variable)
        self.log_activity(
            category="variables",
            action="variable_updated",
            status="success",
            title=f"Variable updated: {variable.name}",
            detail="Updated reusable variable",
            entity_type="variable",
            entity_id=variable.id,
        )
        return variable

    def delete_variable(self, variable_id: str) -> None:
        variable = self.get_variable(variable_id)
        variable_name = variable.name
        self.session.delete(variable)
        self.session.commit()
        self.log_activity(
            category="variables",
            action="variable_deleted",
            status="warning",
            title=f"Variable removed: {variable_name}",
            detail="Deleted reusable variable",
            entity_type="variable",
            entity_id=variable_id,
        )

    def create_channel(self, payload: schemas.ChannelCreate) -> models.Channel:
        validate_channel_payload(payload.platform, payload.auth_type, payload.auth_config, payload.target_config)
        duplicate = self._find_duplicate_channel(payload)
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Channel already exists for {duplicate.target_label or 'this destination'}",
            )
        channel = models.Channel(
            name=payload.name,
            platform=payload.platform,
            status=payload.status,
            auth_type=payload.auth_type,
            credentials=json.dumps(payload.auth_config.values),
            capabilities_json=_model_to_json(payload.capabilities),
            channel_identifier=payload.target_label,
            auth_config_json=_config_to_json(payload.auth_config),
            target_config_json=_config_to_json(payload.target_config),
            provider_account_label=payload.provider_account_label,
            target_label=payload.target_label,
            last_checked=payload.last_checked,
            setup_error=payload.setup_error,
            last_test_status=payload.last_test_status,
        )
        self.session.add(channel)
        self.session.commit()
        self.session.refresh(channel)
        self.log_activity(
            category="channels",
            action="channel_created",
            status="success",
            title=f"Channel added: {channel.name}",
            detail=f"Connected {channel.platform} channel via {channel.auth_type} targeting {channel.target_label or 'configured destination'}",
            entity_type="channel",
            entity_id=channel.id,
            channel_id=channel.id,
            metadata={
                "platform": channel.platform,
                "auth_type": channel.auth_type,
                "target_label": channel.target_label,
                "provider_account_label": channel.provider_account_label,
            },
        )
        return channel

    def get_channel(self, channel_id: str) -> models.Channel:
        channel = self.session.get(models.Channel, channel_id)
        if channel is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
        return channel

    def update_channel(self, channel_id: str, payload: schemas.ChannelUpdate, *, log_activity: bool = True) -> models.Channel:
        channel = self.get_channel(channel_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        auth_config = _coerce_channel_config(updates.get("auth_config"))
        target_config = _coerce_channel_config(updates.get("target_config"))
        if auth_config is not None:
            updates["auth_config"] = auth_config
        if target_config is not None:
            updates["target_config"] = target_config
        next_auth_config = auth_config if auth_config is not None else _load_auth_config(channel)
        next_target_config = target_config if target_config is not None else _load_target_config(channel)
        validate_channel_payload(channel.platform, channel.auth_type, next_auth_config, next_target_config)
        duplicate = self._find_duplicate_channel(
            schemas.ChannelCreate(
                name=updates.get("name", channel.name),
                platform=channel.platform,
                status=updates.get("status", channel.status),
                auth_type=channel.auth_type,
                capabilities=updates.get("capabilities") or schemas.ChannelCapabilities(**json.loads(channel.capabilities_json)),
                auth_config=next_auth_config,
                target_config=next_target_config,
                provider_account_label=updates.get("provider_account_label", channel.provider_account_label),
                target_label=updates.get("target_label", channel.target_label),
                last_checked=updates.get("last_checked", channel.last_checked),
                setup_error=updates.get("setup_error", channel.setup_error),
                last_test_status=updates.get("last_test_status", channel.last_test_status),
            ),
            exclude_id=channel_id,
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Another channel already exists for {duplicate.target_label or 'this destination'}",
            )
        if "capabilities" in updates:
            channel.capabilities_json = _model_to_json(updates.pop("capabilities"))
        if "auth_config" in updates:
            value = updates.pop("auth_config")
            channel.auth_config_json = _config_to_json(value)
            channel.credentials = json.dumps(value.values)
        if "target_config" in updates:
            value = updates.pop("target_config")
            channel.target_config_json = _config_to_json(value)
        if "target_label" in updates:
            channel.channel_identifier = updates["target_label"]
        for key, value in updates.items():
            setattr(channel, key, value)
        self.session.add(channel)
        self.session.commit()
        self.session.refresh(channel)
        return channel

    def _find_duplicate_channel(
        self,
        payload: schemas.ChannelCreate,
        *,
        exclude_id: str | None = None,
    ) -> models.Channel | None:
        candidates = (
            self.session.query(models.Channel)
            .filter(
                models.Channel.platform == payload.platform,
                models.Channel.auth_type == payload.auth_type,
            )
            .all()
        )
        for candidate in candidates:
            if exclude_id is not None and candidate.id == exclude_id:
                continue
            if _load_auth_config(candidate).values != payload.auth_config.values:
                continue
            if _load_target_config(candidate).values != payload.target_config.values:
                continue
            return candidate
        return None

    def _refresh_mirror_group(self, mirror_group_id: str, *, commit: bool = True) -> None:
        if not mirror_group_id:
            return
        assets = (
            self.session.query(models.Asset)
            .filter(models.Asset.mirror_group_id == mirror_group_id)
            .all()
        )
        mirror_count = len(assets)
        for asset in assets:
            asset.mirrors = mirror_count
            self.session.add(asset)
        if commit:
            self.session.commit()

    def delete_channel(self, channel_id: str) -> None:
        channel = self.get_channel(channel_id)
        channel_name = channel.name
        platform = channel.platform
        self.session.delete(channel)
        self.session.commit()
        self.log_activity(
            category="channels",
            action="channel_deleted",
            status="warning",
            title=f"Channel removed: {channel_name}",
            detail=f"Deleted {platform} channel {channel_name}",
            entity_type="channel",
            entity_id=channel_id,
            metadata={"platform": platform},
        )

    def list_assets(self) -> list[models.Asset]:
        assets = self.session.query(models.Asset).order_by(models.Asset.created_at.desc()).all()
        deduped: dict[tuple[str, ...], models.Asset] = {}
        for asset in assets:
            key = _asset_identity(asset)
            existing = deduped.get(key)
            if existing is None or asset.updated_at > existing.updated_at:
                deduped[key] = asset
        return sorted(deduped.values(), key=lambda item: item.created_at, reverse=True)

    def list_messages(self) -> list[models.ChannelMessage]:
        messages = (
            self.session.query(models.ChannelMessage)
            .order_by(*_channel_message_ordering())
            .all()
        )
        deduped: dict[tuple[str, ...], models.ChannelMessage] = {}
        for message in messages:
            key = _channel_message_identity(message)
            existing = deduped.get(key)
            if existing is None or message.updated_at > existing.updated_at:
                deduped[key] = message
        return sorted(
            deduped.values(),
            key=lambda item: (
                item.sent_at or datetime.min,
                item.created_at,
            ),
            reverse=True,
        )

    def create_asset(self, payload: schemas.AssetCreate) -> models.Asset:
        asset = models.Asset(
            channel_id=payload.channel_id,
            platform=payload.platform,
            name=payload.name,
            asset_type=payload.type,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
            provider_asset_id=payload.provider_asset_id,
            provider_message_id=payload.provider_message_id,
            remote_url=payload.remote_url,
            preview_url=payload.preview_url,
            mirror_group_id=payload.mirror_group_id,
            status=payload.status,
            tags_json=json.dumps(payload.tags),
            mirrors=payload.mirrors,
        )
        self.session.add(asset)
        self.session.commit()
        if asset.mirror_group_id:
            self._refresh_mirror_group(asset.mirror_group_id)
        self.session.refresh(asset)
        self.log_activity(
            category="assets",
            action="asset_created",
            status="success",
            title=f"Asset indexed: {asset.name}",
            detail=f"Added remote {asset.asset_type} asset from {asset.platform}",
            entity_type="asset",
            entity_id=asset.id,
            channel_id=asset.channel_id,
            metadata={"platform": asset.platform, "size_bytes": asset.size_bytes},
        )
        return asset

    def create_remote_assets(self, payloads: list[schemas.AssetCreate]) -> list[models.Asset]:
        created: list[models.Asset] = []
        mirror_group_ids: set[str] = set()
        for payload in payloads:
            asset = self._upsert_remote_asset(payload)
            created.append(asset)
            if payload.mirror_group_id:
                mirror_group_ids.add(payload.mirror_group_id)
        if created:
            self.session.commit()
            for mirror_group_id in mirror_group_ids:
                self._refresh_mirror_group(mirror_group_id, commit=False)
            if mirror_group_ids:
                self.session.commit()
            for asset in created:
                self.session.refresh(asset)
        return created

    def get_asset(self, asset_id: str) -> models.Asset:
        asset = self.session.get(models.Asset, asset_id)
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
        if asset.channel is None and asset.channel_id:
            asset.channel = self.session.get(models.Channel, asset.channel_id)
        return asset

    def update_asset(self, asset_id: str, payload: schemas.AssetUpdate) -> models.Asset:
        asset = self.get_asset(asset_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        update_keys = sorted(updates.keys())
        if "tags" in updates:
            asset.tags_json = json.dumps(updates.pop("tags"))
        for key, value in updates.items():
            setattr(asset, key, value)
        self.session.add(asset)
        self.session.commit()
        self.session.refresh(asset)
        if update_keys:
            self.log_activity(
                category="assets",
                action="asset_updated",
                status="success",
                title=f"Asset updated: {asset.name}",
                detail=f"Updated {', '.join(update_keys)} for asset {asset.name}",
                entity_type="asset",
                entity_id=asset.id,
                channel_id=asset.channel_id,
                metadata={"updated_fields": update_keys, "platform": asset.platform},
            )
        return asset

    def mirror_asset(
        self,
        asset_id: str,
        channel_ids: list[str],
    ) -> schemas.AssetMirrorResponse:
        asset = self.get_asset(asset_id)
        normalized_channel_ids = list(dict.fromkeys(channel_id for channel_id in channel_ids if channel_id.strip()))
        if not normalized_channel_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Select at least one channel to mirror this asset to",
            )

        mirror_group_id = asset.mirror_group_id or uuid4().hex
        if not asset.mirror_group_id:
            asset.mirror_group_id = mirror_group_id
            self.session.add(asset)
            self.session.commit()
            self._refresh_mirror_group(mirror_group_id)
            self.session.refresh(asset)

        content, mime_type = fetch_asset_content(asset)
        attachment = schemas.BroadcastAttachment(
            name=asset.name,
            type=asset.asset_type,
            size=asset.size_bytes,
            mime_type=mime_type or asset.mime_type,
            content_base64=base64.b64encode(content).decode("utf-8"),
            mirror_group_id=mirror_group_id,
        )

        existing_channel_ids = {
            item.channel_id
            for item in self.session.query(models.Asset)
            .filter(models.Asset.mirror_group_id == mirror_group_id)
            .all()
            if item.channel_id
        }

        results: list[schemas.AssetMirrorChannelResult] = []
        remote_assets: list[schemas.AssetCreate] = []
        mirrored_count = 0
        skipped_count = 0
        any_success = False

        for channel_id in normalized_channel_ids:
            channel = self.get_channel(channel_id)
            if channel_id in existing_channel_ids:
                skipped_count += 1
                results.append(
                    schemas.AssetMirrorChannelResult(
                        channel_id=channel.id,
                        channel_name=channel.name,
                        status="skipped",
                    message="Asset is already mirrored to this channel",
                    )
                )
                continue

            result, detail, created_assets = send_channel_message(
                channel,
                attachments=[attachment],
            )
            next_status = "active" if result == "success" else "error"
            updated_channel = self.update_channel(
                channel_id,
                schemas.ChannelUpdate(
                    status=next_status,
                    last_checked=datetime.utcnow(),
                    last_test_status=result,
                    setup_error="" if result == "success" else detail,
                ),
            )
            if result == "success":
                any_success = True
                mirrored_count += 1
                remote_assets.extend(created_assets)
                existing_channel_ids.add(channel_id)
            results.append(
                schemas.AssetMirrorChannelResult(
                    channel_id=updated_channel.id,
                    channel_name=updated_channel.name,
                    status=result,
                    message=detail,
                )
            )

        created_records = self.create_remote_assets(remote_assets) if remote_assets else []
        if mirror_group_id and (created_records or mirrored_count > 0):
            self._refresh_mirror_group(mirror_group_id)

        overall_status = "success" if mirrored_count and all(item.status == "success" for item in results if item.status != "skipped") else (
            "partial" if any_success else "failed"
        )
        self.log_activity(
            category="assets",
            action="asset_mirrored" if mirrored_count else "asset_mirror_attempted",
            status="success" if overall_status == "success" else ("warning" if overall_status == "partial" else "error"),
            title=f"Asset mirrored: {asset.name}",
            detail=(
                f"Mirrored asset to {mirrored_count} channel(s); skipped {skipped_count} existing mirror(s)"
            ),
            entity_type="asset",
            entity_id=asset.id,
            channel_id=asset.channel_id,
            metadata={
                "mirror_group_id": mirror_group_id,
                "mirrored_count": mirrored_count,
                "skipped_count": skipped_count,
            },
        )
        return schemas.AssetMirrorResponse(
            status=overall_status,
            mirrored_count=mirrored_count,
            skipped_count=skipped_count,
            results=results,
        )

    def delete_asset(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        asset_name = asset.name
        platform = asset.platform
        channel_id = asset.channel_id
        mirror_group_id = asset.mirror_group_id
        deleted_remote = True
        try:
            delete_remote_asset(asset)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                deleted_remote = False
            else:
                raise
        self.session.delete(asset)
        self.session.commit()
        if mirror_group_id:
            self._refresh_mirror_group(mirror_group_id)
        self.log_activity(
            category="assets",
            action="asset_deleted" if deleted_remote else "asset_deleted_missing_remote",
            status="warning",
            title=f"Asset deleted: {asset_name}",
            detail=(
                f"Deleted asset {asset_name} from Assets and the {platform} provider"
                if deleted_remote
                else f"Deleted asset {asset_name} from Assets after confirming it was already missing on {platform}"
            ),
            entity_type="asset",
            entity_id=asset_id,
            channel_id=channel_id,
            metadata={"platform": platform, "deleted_remote": deleted_remote},
        )

    def sync_remote_messages(self) -> dict[str, int]:
        synced_messages = 0
        synced_channels = 0
        skipped_channels = 0
        active_channels = self.session.query(models.Channel).filter(models.Channel.status == "active").all()
        telegram_groups: dict[str, list[models.Channel]] = {}
        non_telegram_channels: list[models.Channel] = []
        for channel in active_channels:
            if channel.platform == "telegram" and channel.auth_type == "bot":
                bot_token = _load_auth_config(channel).get_str("bot_token")
                if not bot_token:
                    skipped_channels += 1
                    continue
                telegram_groups.setdefault(bot_token, []).append(channel)
            else:
                non_telegram_channels.append(channel)

        for channels in telegram_groups.values():
            try:
                created = self.sync_telegram_bot_messages(channels)
            except Exception as exc:
                self.session.rollback()
                skipped_channels += len(channels)
                for channel in channels:
                    self.log_activity(
                        category="messages",
                        action="message_sync_skipped",
                        status="warning",
                        title=f"Message sync skipped: {channel.name}",
                        detail=f"Skipped message sync for {channel.platform} channel due to provider error: {exc}",
                        entity_type="channel",
                        entity_id=channel.id,
                        channel_id=channel.id,
                        metadata={"platform": channel.platform, "error": str(exc)},
                    )
                continue
            synced_channels += len(channels)
            synced_messages += created

        for channel in non_telegram_channels:
            try:
                created = self.sync_channel_messages(channel)
            except Exception as exc:
                self.session.rollback()
                skipped_channels += 1
                self.log_activity(
                    category="messages",
                    action="message_sync_skipped",
                    status="warning",
                    title=f"Message sync skipped: {channel.name}",
                    detail=f"Skipped message sync for {channel.platform} channel due to provider error: {exc}",
                    entity_type="channel",
                    entity_id=channel.id,
                    channel_id=channel.id,
                    metadata={"platform": channel.platform, "error": str(exc)},
                )
                continue
            if created is None:
                skipped_channels += 1
                continue
            synced_channels += 1
            synced_messages += created
        return {
            "synced_channels": synced_channels,
            "synced_messages": synced_messages,
            "skipped_channels": skipped_channels,
        }

    def sync_remote_assets(self) -> dict[str, int]:
        synced_assets = 0
        synced_channels = 0
        skipped_channels = 0
        for channel in self.session.query(models.Channel).filter(models.Channel.status == "active").all():
            try:
                created = self.sync_channel_assets(channel)
            except Exception as exc:
                self.session.rollback()
                skipped_channels += 1
                self.log_activity(
                    category="assets",
                    action="asset_sync_skipped",
                    status="warning",
                    title=f"Asset sync skipped: {channel.name}",
                    detail=f"Skipped asset sync for {channel.platform} channel due to provider error: {exc}",
                    entity_type="channel",
                    entity_id=channel.id,
                    channel_id=channel.id,
                    metadata={"platform": channel.platform, "error": str(exc)},
                )
                continue
            if created is None:
                skipped_channels += 1
                continue
            synced_channels += 1
            synced_assets += created
        return {
            "synced_channels": synced_channels,
            "synced_assets": synced_assets,
            "skipped_channels": skipped_channels,
        }

    def sync_channel_assets(self, channel: models.Channel) -> int | None:
        created_assets: list[schemas.AssetCreate]
        reconcile_missing = False
        if channel.platform == "slack" and channel.auth_type == "bot":
            created_assets = _sync_slack_channel_assets(channel)
            reconcile_missing = True
        elif channel.platform == "discord" and channel.auth_type == "bot":
            created_assets = _sync_discord_channel_assets(channel)
            reconcile_missing = True
        elif channel.platform == "mattermost" and channel.auth_type == "bot":
            created_assets = _sync_mattermost_channel_assets(channel)
        elif channel.platform == "rocketchat" and channel.auth_type == "bot":
            created_assets = _sync_rocketchat_channel_assets(channel)
        elif channel.platform == "telegram" and channel.auth_type == "bot":
            created_assets = _sync_telegram_bot_assets(channel)
        else:
            return None

        for payload in created_assets:
            self._upsert_remote_asset(payload)
        removed_assets = self._remove_missing_channel_assets(channel, created_assets) if reconcile_missing else 0
        channel.asset_last_synced_at = datetime.utcnow()
        self.session.add(channel)
        self.session.commit()
        if removed_assets:
            self.log_activity(
                category="assets",
                action="asset_sync_reconciled",
                status="warning",
                title=f"Asset sync reconciled: {channel.name}",
                detail=f"Removed {removed_assets} asset(s) that no longer exist on {channel.platform}",
                entity_type="channel",
                entity_id=channel.id,
                channel_id=channel.id,
                metadata={"platform": channel.platform, "removed_assets": removed_assets},
            )
        return len(created_assets)

    def sync_channel_messages(self, channel: models.Channel) -> int | None:
        created_messages: list[schemas.ChannelMessageCreate]
        if channel.platform == "slack" and channel.auth_type == "bot":
            created_messages = _sync_slack_channel_messages(channel)
        elif channel.platform == "discord" and channel.auth_type == "bot":
            created_messages = _sync_discord_channel_messages(channel)
        elif channel.platform == "mattermost" and channel.auth_type == "bot":
            created_messages = _sync_mattermost_channel_messages(channel)
        elif channel.platform == "rocketchat" and channel.auth_type == "bot":
            created_messages = _sync_rocketchat_channel_messages(channel)
        else:
            return None

        for payload in created_messages:
            self._upsert_channel_message(payload)
        channel.message_last_synced_at = datetime.utcnow()
        self.session.add(channel)
        self.session.commit()
        return len(created_messages)

    def sync_telegram_bot_messages(self, channels: list[models.Channel]) -> int:
        if not channels:
            return 0
        auth_config = _load_auth_config(channels[0])
        token = auth_config.get_str("bot_token")
        if not token:
            return 0

        target_channels = {
            _require_string(_load_target_config(channel).values.get("chat_id"), "chat_id"): channel
            for channel in channels
        }
        offsets = [int(channel.message_sync_cursor) for channel in channels if channel.message_sync_cursor.strip().isdigit()]
        next_offset = max(offsets) if offsets else 0
        created_count = 0
        highest_offset = next_offset
        payload = _sync_telegram_bot_messages(token, offset=next_offset)
        for message_payload in payload["messages"]:
            provider_chat_id = str(message_payload.get("chat", {}).get("id", ""))
            if provider_chat_id not in target_channels:
                continue
            bound_payload = _build_telegram_message_payload(target_channels[provider_chat_id], message_payload)
            if bound_payload is None:
                continue
            self._upsert_channel_message(bound_payload)
            created_count += 1
        if payload["next_offset"] > highest_offset:
            highest_offset = payload["next_offset"]
        now = datetime.utcnow()
        for channel in channels:
            if highest_offset:
                channel.message_sync_cursor = str(highest_offset)
            channel.message_last_synced_at = now
            self.session.add(channel)
        self.session.commit()
        return created_count

    def _upsert_remote_asset(self, payload: schemas.AssetCreate) -> models.Asset:
        assets = (
            self.session.query(models.Asset)
            .filter(
                models.Asset.platform == payload.platform,
                models.Asset.provider_asset_id == payload.provider_asset_id,
            )
            .filter(
                or_(
                    models.Asset.channel_id == payload.channel_id,
                    models.Asset.channel_id.is_(None),
                )
            )
            .order_by(models.Asset.created_at.asc())
            .all()
        )
        asset = assets[0] if assets else None
        if asset is None:
            asset = models.Asset(
                channel_id=payload.channel_id,
                platform=payload.platform,
                name=payload.name,
                asset_type=payload.type,
                mime_type=payload.mime_type,
                size_bytes=payload.size_bytes,
                provider_asset_id=payload.provider_asset_id,
                remote_url=payload.remote_url,
                preview_url=payload.preview_url,
                mirror_group_id=payload.mirror_group_id,
                status=payload.status,
                tags_json=json.dumps(payload.tags),
                mirrors=payload.mirrors,
            )
            self.session.add(asset)
            return asset

        asset.channel_id = payload.channel_id
        asset.name = payload.name
        asset.asset_type = payload.type
        asset.mime_type = payload.mime_type
        asset.size_bytes = payload.size_bytes
        asset.remote_url = payload.remote_url
        asset.preview_url = payload.preview_url
        if payload.provider_message_id:
            asset.provider_message_id = payload.provider_message_id
        if payload.mirror_group_id:
            asset.mirror_group_id = payload.mirror_group_id
        asset.status = payload.status
        if payload.mirrors:
            asset.mirrors = payload.mirrors
        self.session.add(asset)
        for duplicate in assets[1:]:
            self.session.delete(duplicate)
        return asset

    def _upsert_channel_message(self, payload: schemas.ChannelMessageCreate) -> models.ChannelMessage:
        messages = (
            self.session.query(models.ChannelMessage)
            .filter(
                models.ChannelMessage.platform == payload.platform,
                models.ChannelMessage.provider_message_id == payload.provider_message_id,
            )
            .filter(
                or_(
                    models.ChannelMessage.provider_chat_id == payload.provider_chat_id,
                    models.ChannelMessage.provider_chat_id == "",
                )
            )
            .order_by(models.ChannelMessage.created_at.asc())
            .all()
        )
        message = messages[0] if messages else None
        if message is None:
            message = models.ChannelMessage(
                channel_id=payload.channel_id,
                platform=payload.platform,
                provider_chat_id=payload.provider_chat_id,
                provider_message_id=payload.provider_message_id,
                author_name=payload.author_name,
                content=payload.content,
                attachment_count=payload.attachment_count,
                external_url=payload.external_url,
                sent_at=payload.sent_at,
            )
            self.session.add(message)
            return message

        message.channel_id = payload.channel_id
        message.provider_chat_id = payload.provider_chat_id
        message.author_name = payload.author_name
        message.content = payload.content
        message.attachment_count = payload.attachment_count
        message.external_url = payload.external_url
        message.sent_at = payload.sent_at
        self.session.add(message)
        for duplicate in messages[1:]:
            self.session.delete(duplicate)
        return message

    def _remove_missing_channel_assets(
        self,
        channel: models.Channel,
        remote_assets: list[schemas.AssetCreate],
    ) -> int:
        # Flush pending duplicate cleanup from _upsert_remote_asset() so the
        # reconciliation query does not try to delete the same rows again.
        self.session.flush()
        remote_identities = {_asset_payload_identity(asset) for asset in remote_assets}
        local_assets = (
            self.session.query(models.Asset)
            .filter(
                models.Asset.channel_id == channel.id,
                models.Asset.platform == channel.platform,
            )
            .all()
        )
        removed = 0
        for asset in local_assets:
            if _asset_identity(asset) in remote_identities:
                continue
            self.session.delete(asset)
            removed += 1
        return removed

    def list_api_keys(self) -> list[models.ApiKey]:
        return self.session.query(models.ApiKey).order_by(models.ApiKey.created_at.desc()).all()

    def create_api_key(self, payload: schemas.ApiKeyCreate) -> models.ApiKey:
        api_key = models.ApiKey(**_model_to_dict(payload))
        self.session.add(api_key)
        self.session.commit()
        self.session.refresh(api_key)
        return api_key

    def update_api_key(self, api_key_id: str, payload: schemas.ApiKeyUpdate) -> models.ApiKey:
        api_key = self.session.get(models.ApiKey, api_key_id)
        if api_key is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
        for key, value in _model_to_dict(payload, exclude_unset=True).items():
            setattr(api_key, key, value)
        self.session.add(api_key)
        self.session.commit()
        self.session.refresh(api_key)
        return api_key

    def delete_api_key(self, api_key_id: str) -> None:
        api_key = self.session.get(models.ApiKey, api_key_id)
        if api_key is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
        self.session.delete(api_key)
        self.session.commit()

    def list_webhooks(self) -> list[models.Webhook]:
        return self.session.query(models.Webhook).order_by(models.Webhook.created_at.desc()).all()

    def create_webhook(self, payload: schemas.WebhookCreate) -> models.Webhook:
        if payload.channel_id is not None:
            self.get_channel(payload.channel_id)
        webhook = models.Webhook(**_model_to_dict(payload))
        self.session.add(webhook)
        self.session.commit()
        self.session.refresh(webhook)
        return webhook

    def update_webhook(self, webhook_id: str, payload: schemas.WebhookUpdate) -> models.Webhook:
        webhook = self.session.get(models.Webhook, webhook_id)
        if webhook is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        updates = _model_to_dict(payload, exclude_unset=True)
        if "channel_id" in updates and updates["channel_id"] is not None:
            self.get_channel(updates["channel_id"])
        for key, value in updates.items():
            setattr(webhook, key, value)
        self.session.add(webhook)
        self.session.commit()
        self.session.refresh(webhook)
        return webhook

    def delete_webhook(self, webhook_id: str) -> None:
        webhook = self.session.get(models.Webhook, webhook_id)
        if webhook is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        self.session.delete(webhook)
        self.session.commit()


def settings_to_schema_with_config(settings: models.AppSettings, config: AppConfig) -> schemas.SettingsResponse:
    current_local_db_path = ""
    default_local_db_path = ""
    if config.database_engine == "sqlite":
        current_local_db_path = str(Path(config.database_url.removeprefix("sqlite:///")).expanduser().resolve())
        default_local_db_path = str(config.default_database_path.resolve())
    return schemas.SettingsResponse(
        id=settings.id,
        push_confirmations=_coerce_bool(settings.push_confirmations, True),
        error_alerts=_coerce_bool(settings.error_alerts, True),
        compact_mode=_coerce_bool(settings.compact_mode, False),
        keyboard_shortcuts=_coerce_bool(settings.keyboard_shortcuts, True),
        dock_magnification=_coerce_bool(settings.dock_magnification, False),
        dock_auto_hide=_coerce_bool(settings.dock_auto_hide, False),
        channel_test_message=_coerce_str(settings.channel_test_message),
        storage_engine=settings.storage_engine or "local",
        db_connection=schemas.DbConnectionConfig(
            host=_coerce_str(settings.db_host),
            port=_coerce_str(settings.db_port),
            database=_coerce_str(settings.db_database),
            username=_coerce_str(settings.db_username),
            password=_coerce_str(settings.db_password),
        ),
        local_db_path=_coerce_str(settings.local_db_directory) or current_local_db_path,
        current_local_db_path=current_local_db_path,
        default_local_db_path=default_local_db_path,
        created_at=settings.created_at,
        updated_at=settings.updated_at,
    )


def _coerce_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _coerce_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _config_to_json(config: schemas.ChannelConfig) -> str:
    return json.dumps(config.values, sort_keys=True)


def asset_to_schema(asset: models.Asset) -> schemas.AssetResponse:
    return schemas.AssetResponse(
        id=asset.id,
        channel_id=asset.channel_id,
        platform=asset.platform,
        name=asset.name,
        type=asset.asset_type,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        provider_asset_id=asset.provider_asset_id,
        provider_message_id=asset.provider_message_id,
        remote_url=asset.remote_url,
        preview_url=asset.preview_url,
        mirror_group_id=asset.mirror_group_id,
        status=asset.status,
        tags=json.loads(asset.tags_json),
        mirrors=asset.mirrors,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def message_to_schema(message: models.ChannelMessage) -> schemas.ChannelMessageResponse:
    return schemas.ChannelMessageResponse(
        id=message.id,
        channel_id=message.channel_id,
        platform=message.platform,
        provider_chat_id=message.provider_chat_id,
        provider_message_id=message.provider_message_id,
        author_name=message.author_name,
        content=message.content,
        attachment_count=message.attachment_count,
        external_url=message.external_url,
        sent_at=message.sent_at,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def activity_log_to_schema(activity: models.ActivityLog) -> schemas.ActivityLogResponse:
    return schemas.ActivityLogResponse(
        id=activity.id,
        category=activity.category,
        action=activity.action,
        status=activity.status,
        title=activity.title,
        detail=activity.detail,
        entity_type=activity.entity_type,
        entity_id=activity.entity_id,
        channel_id=activity.channel_id,
        metadata=json.loads(activity.metadata_json),
        created_at=activity.created_at,
    )


def channel_to_schema(channel: models.Channel) -> schemas.ChannelResponse:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    return schemas.ChannelResponse(
        id=channel.id,
        name=channel.name,
        platform=channel.platform,
        status=channel.status,
        auth_type=channel.auth_type,
        auth_config=auth_config,
        target_config=target_config,
        capabilities=schemas.ChannelCapabilities(**json.loads(channel.capabilities_json)),
        provider_account_label=channel.provider_account_label,
        target_label=channel.target_label or channel.channel_identifier or "",
        last_checked=channel.last_checked,
        setup_error=channel.setup_error,
        last_test_status=_normalize_last_test_status(channel.last_test_status),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _normalize_last_test_status(value: str) -> schemas.LastTestStatus:
    if value == "untested":
        return ""
    return value  # type: ignore[return-value]


def validate_channel_payload(
    platform: schemas.Platform,
    auth_type: schemas.AuthType,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> None:
    setup = get_provider_setup(platform, auth_type)
    missing_auth = [
        field for field in setup.auth_fields if not _has_config_value(auth_config.values.get(field))
    ]
    missing_target = [
        field for field in setup.target_fields if not _has_config_value(target_config.values.get(field))
    ]
    if missing_auth or missing_target:
        missing = ", ".join([*missing_auth, *missing_target])
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required provider setup fields: {missing}",
        )
def test_channel_configuration(
    channel: models.Channel | schemas.ChannelCreate,
) -> tuple[schemas.LastTestStatus, str]:
    auth_config, target_config, platform, auth_type = _resolve_channel_test_payload(channel)
    validate_channel_payload(platform, auth_type, auth_config, target_config)
    try:
        if platform == "slack" and auth_type == "webhook":
            return _test_slack_webhook(auth_config)
        if platform == "slack" and auth_type == "bot":
            return _test_slack_bot(auth_config, target_config)
        if platform == "mattermost" and auth_type == "webhook":
            return _test_mattermost_webhook(auth_config)
        if platform == "mattermost" and auth_type == "bot":
            return _test_mattermost_bot(auth_config, target_config)
        if platform == "rocketchat" and auth_type == "webhook":
            return _test_rocketchat_webhook(auth_config)
        if platform == "rocketchat" and auth_type == "bot":
            return _test_rocketchat_bot(auth_config, target_config)
        if platform == "discord" and auth_type == "bot":
            return _test_discord_bot(auth_config, target_config)
        if platform == "telegram" and auth_type == "bot":
            return _test_telegram_bot(auth_config, target_config)
        setup = get_provider_setup(platform, auth_type)
        return "success", setup.test_message
    except HTTPException:
        raise
    except ProviderConnectivityError as exc:
        return "failed", f"Provider connectivity failed: {exc}"
    except error.URLError as exc:
        return "failed", f"Provider connectivity failed: {_provider_connectivity_message(exc)}"
    except Exception as exc:
        return "failed", f"Provider test failed: {exc}"


def _load_auth_config(channel: models.Channel) -> schemas.ChannelConfig:
    if channel.auth_config_json:
        return schemas.ChannelConfig(values=json.loads(channel.auth_config_json))
    if channel.credentials:
        return schemas.ChannelConfig(values=_legacy_auth_config(channel))
    return schemas.ChannelConfig()


def _load_target_config(channel: models.Channel) -> schemas.ChannelConfig:
    if channel.target_config_json:
        return schemas.ChannelConfig(values=json.loads(channel.target_config_json))
    if channel.channel_identifier:
        return schemas.ChannelConfig(values={"legacy_target": channel.channel_identifier})
    return schemas.ChannelConfig()


def _resolve_channel_test_payload(
    channel: models.Channel | schemas.ChannelCreate,
) -> tuple[schemas.ChannelConfig, schemas.ChannelConfig, schemas.Platform, schemas.AuthType]:
    if isinstance(channel, models.Channel):
        return (
            _load_auth_config(channel),
            _load_target_config(channel),
            channel.platform,
            channel.auth_type,
        )
    return (
        channel.auth_config,
        channel.target_config,
        channel.platform,
        channel.auth_type,
    )


def _legacy_auth_config(channel: models.Channel) -> dict[str, str]:
    setup = get_provider_setup(channel.platform, channel.auth_type)
    if len(setup.auth_fields) == 1:
        return {setup.auth_fields[0]: channel.credentials}
    return {"legacy_value": channel.credentials}


def _has_config_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() for item in value)
    return value is not None


def _decode_asset_content(content_base64: str, asset_name: str) -> bytes:
    try:
        return base64.b64decode(content_base64)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Asset '{asset_name}' is not valid base64 data",
        ) from exc


def _normalize_asset_type(raw_type: str, mime_type: str) -> schemas.AssetType:
    if raw_type in {"document", "image", "video", "audio", "text", "transcript", "recording"}:
        return raw_type  # type: ignore[return-value]
    lower_mime = mime_type.lower()
    if lower_mime.startswith("image/"):
        return "image"
    if lower_mime.startswith("video/"):
        return "video"
    if lower_mime.startswith("audio/"):
        return "audio"
    if lower_mime.startswith("text/"):
        return "text"
    return "document"


def _build_remote_asset_payload(
    channel: models.Channel | schemas.ChannelCreate,
    *,
    provider_asset_id: str,
    provider_message_id: str = "",
    remote_url: str,
    preview_url: str,
    name: str,
    mime_type: str,
    size_bytes: int,
    asset_type: str,
    mirror_group_id: str = "",
) -> schemas.AssetCreate:
    auth_config, _, platform, _ = _resolve_channel_test_payload(channel)
    del auth_config
    channel_id = channel.id if isinstance(channel, models.Channel) else None
    return schemas.AssetCreate(
        channel_id=channel_id,
        platform=platform,
        name=name,
        type=_normalize_asset_type(asset_type, mime_type),
        mime_type=mime_type,
        size_bytes=size_bytes,
        provider_asset_id=provider_asset_id,
        provider_message_id=provider_message_id,
        remote_url=remote_url,
        preview_url=preview_url or remote_url,
        mirror_group_id=mirror_group_id,
        status="synced",
        tags=[],
        mirrors=1,
    )


def _build_channel_message_payload(
    channel: models.Channel | schemas.ChannelCreate,
    *,
    provider_chat_id: str = "",
    provider_message_id: str,
    content: str,
    sent_at: datetime | None,
    author_name: str = "",
    attachment_count: int = 0,
    external_url: str = "",
) -> schemas.ChannelMessageCreate:
    _, _, platform, _ = _resolve_channel_test_payload(channel)
    channel_id = channel.id if isinstance(channel, models.Channel) else None
    normalized_content = content.strip()
    return schemas.ChannelMessageCreate(
        channel_id=channel_id,
        platform=platform,
        provider_chat_id=provider_chat_id,
        provider_message_id=provider_message_id,
        author_name=author_name.strip(),
        content=normalized_content,
        attachment_count=max(0, attachment_count),
        external_url=external_url,
        sent_at=sent_at,
    )


def _datetime_from_unix_timestamp(raw_value: str | int | float | None) -> datetime | None:
    if raw_value in {None, ""}:
        return None
    try:
        return datetime.fromtimestamp(float(raw_value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _datetime_from_iso_timestamp(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    normalized = raw_value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _datetime_from_milliseconds(raw_value: object) -> datetime | None:
    try:
        milliseconds = int(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).replace(tzinfo=None)


def _discord_timestamp_from_snowflake(raw_value: str) -> datetime | None:
    try:
        snowflake = int(raw_value)
    except (TypeError, ValueError):
        return None
    unix_milliseconds = (snowflake >> 22) + 1420070400000
    return datetime.fromtimestamp(unix_milliseconds / 1000, tz=timezone.utc).replace(tzinfo=None)


def _channel_message_identity(message: models.ChannelMessage) -> tuple[str, ...]:
    if message.provider_chat_id and message.provider_message_id:
        return (message.platform, message.provider_chat_id, message.provider_message_id)
    if message.platform == "discord" and message.provider_message_id:
        return (message.platform, message.provider_message_id)
    sent_at = message.sent_at.isoformat() if message.sent_at else ""
    return (
        message.platform,
        message.channel_id or "",
        message.provider_message_id,
        message.content,
        sent_at,
    )


def _asset_identity(asset: models.Asset) -> tuple[str, ...]:
    if asset.provider_asset_id:
        return (asset.platform, asset.provider_asset_id)
    if asset.remote_url:
        return (asset.platform, asset.remote_url)
    return (asset.platform, asset.channel_id or "", asset.name, asset.mime_type, str(asset.size_bytes))


def _asset_payload_identity(asset: schemas.AssetCreate) -> tuple[str, ...]:
    if asset.provider_asset_id:
        return (asset.platform, asset.provider_asset_id)
    if asset.remote_url:
        return (asset.platform, asset.remote_url)
    return (asset.platform, asset.channel_id or "", asset.name, asset.mime_type, str(asset.size_bytes))


def _telegram_author_name(message: dict[str, object]) -> str:
    sender_chat = message.get("sender_chat")
    if isinstance(sender_chat, dict):
        return str(sender_chat.get("title") or sender_chat.get("username") or sender_chat.get("id") or "")
    user = message.get("from")
    if not isinstance(user, dict):
        return ""
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part)
    return full_name or username or str(user.get("id") or "")


def _telegram_attachment_count(message: dict[str, object]) -> int:
    count = 0
    attachment_keys = [
        "document",
        "video",
        "audio",
        "voice",
        "video_note",
        "animation",
        "sticker",
        "contact",
        "location",
        "venue",
        "poll",
    ]
    for key in attachment_keys:
        if key in message and message.get(key) is not None:
            count += 1
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        count += 1
    return count


def _build_telegram_message_payload(
    channel: models.Channel,
    message: dict[str, object],
) -> schemas.ChannelMessageCreate | None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    provider_chat_id = str(chat.get("id", "")).strip()
    provider_message_id = str(message.get("message_id", "")).strip()
    if not provider_chat_id or not provider_message_id:
        return None
    content = str(message.get("text") or message.get("caption") or "")
    attachment_count = _telegram_attachment_count(message)
    if not content.strip() and attachment_count == 0:
        return None
    chat_username = str(chat.get("username") or "").strip()
    external_url = f"https://t.me/{chat_username}/{provider_message_id}" if chat_username else ""
    return _build_channel_message_payload(
        channel,
        provider_chat_id=provider_chat_id,
        provider_message_id=provider_message_id,
        content=content,
        sent_at=_datetime_from_unix_timestamp(message.get("date")),
        author_name=_telegram_author_name(message),
        attachment_count=attachment_count,
        external_url=external_url,
    )


def _telegram_file_from_message(message: dict[str, object]) -> tuple[dict[str, object], str, str, str] | None:
    file_fields = [
        ("document", "document"),
        ("video", "video"),
        ("audio", "audio"),
        ("voice", "audio"),
        ("video_note", "video"),
        ("animation", "video"),
        ("sticker", "image"),
    ]
    for field_name, asset_type in file_fields:
        value = message.get(field_name)
        if isinstance(value, dict):
            file_name = str(value.get("file_name") or f"telegram-{field_name}")
            mime_type = str(value.get("mime_type") or "application/octet-stream")
            return value, file_name, mime_type, asset_type
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = photos[-1]
        if isinstance(photo, dict):
            return photo, "telegram-photo.jpg", "image/jpeg", "image"
    return None


def _build_telegram_asset_from_message(
    channel: models.Channel | schemas.ChannelCreate,
    message: dict[str, object],
) -> schemas.AssetCreate | None:
    provider_message_id = str(message.get("message_id", "")).strip()
    if not provider_message_id:
        return None
    file_item = _telegram_file_from_message(message)
    if file_item is None:
        return None
    telegram_file, file_name, mime_type, asset_type = file_item
    provider_asset_id = str(telegram_file.get("file_id") or "").strip()
    if not provider_asset_id:
        return None
    return _build_remote_asset_payload(
        channel,
        provider_asset_id=provider_asset_id,
        provider_message_id=provider_message_id,
        remote_url="",
        preview_url="",
        name=file_name,
        mime_type=mime_type,
        size_bytes=int(telegram_file.get("file_size") or 0),
        asset_type=asset_type,
    )


def fetch_asset_content(asset: models.Asset) -> tuple[bytes, str]:
    if asset.platform == "slack":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slack asset is not linked to a channel")
        token = _load_auth_config(asset.channel).get_str("bot_token")
        if not token or not asset.remote_url:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slack asset is missing retrieval credentials")
        try:
            return (
                _perform_request(
                    asset.remote_url,
                    method="GET",
                    headers={"Authorization": f"Bearer {token}"},
                ),
                asset.mime_type,
            )
        except RuntimeError as exc:
            _raise_missing_remote_asset(asset, exc)
    if asset.platform == "telegram":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Telegram asset is not linked to a channel")
        token = _load_auth_config(asset.channel).get_str("bot_token")
        if not token or not asset.provider_asset_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Telegram asset is missing retrieval credentials")
        payload = _telegram_api_request(
            f"https://api.telegram.org/bot{token}/getFile?{parse.urlencode({'file_id': asset.provider_asset_id})}"
        )
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description", "Telegram getFile failed")))
        file_path = payload.get("result", {}).get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram did not return a file path for this asset")
        return (
            _perform_request(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                method="GET",
            ),
            asset.mime_type,
        )
    if asset.platform == "mattermost":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mattermost asset is not linked to a channel")
        auth_config = _load_auth_config(asset.channel)
        token = auth_config.get_str("access_token")
        base_url = _normalize_server_url(auth_config.get_str("server_url"))
        download_url = f"{base_url}/api/v4/files/{asset.provider_asset_id}" if asset.provider_asset_id else asset.remote_url
        if not token or not download_url:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mattermost asset is missing retrieval credentials")
        try:
            return (
                _perform_request(
                    download_url,
                    method="GET",
                    headers={"Authorization": f"Bearer {token}"},
                ),
                asset.mime_type,
            )
        except RuntimeError as exc:
            _raise_missing_remote_asset(asset, exc)
    if asset.platform == "rocketchat":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Rocket.Chat asset is not linked to a channel")
        auth_config = _load_auth_config(asset.channel)
        auth_token = auth_config.get_str("auth_token")
        user_id = auth_config.get_str("user_id")
        if not auth_token or not user_id or not asset.remote_url:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Rocket.Chat asset is missing retrieval credentials")
        try:
            return (
                _perform_request(
                    asset.remote_url,
                    method="GET",
                    headers={
                        "X-Auth-Token": auth_token,
                        "X-User-Id": user_id,
                    },
                ),
                asset.mime_type,
            )
        except RuntimeError as exc:
            _raise_missing_remote_asset(asset, exc)
    if asset.platform == "discord":
        if not asset.remote_url:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Discord asset is missing a remote URL")
        try:
            return _perform_request(asset.remote_url, method="GET"), asset.mime_type
        except RuntimeError as exc:
            _raise_missing_remote_asset(asset, exc)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Remote asset retrieval is not implemented for {asset.platform}",
    )


def delete_remote_asset(asset: models.Asset) -> None:
    if asset.platform == "slack":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slack asset is not linked to a channel")
        token = _load_auth_config(asset.channel).get_str("bot_token")
        if not token or not asset.provider_asset_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slack asset is missing delete credentials")
        payload = _slack_api_request(
            "files.delete",
            token,
            {"file": asset.provider_asset_id},
            method="POST",
        )
        if not payload.get("ok"):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_format_slack_error(payload, "Slack files.delete failed"))
        return
    if asset.platform == "telegram":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Telegram asset is not linked to a channel")
        token = _load_auth_config(asset.channel).get_str("bot_token")
        target_config = _load_target_config(asset.channel)
        chat_id = _require_string(target_config.values.get("chat_id"), "chat_id")
        if not token or not asset.provider_message_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Telegram asset is missing delete credentials")
        payload = _telegram_api_request(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            method="POST",
            body=parse.urlencode({"chat_id": chat_id, "message_id": asset.provider_message_id}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not payload.get("ok"):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(payload.get("description", "Telegram deleteMessage failed")))
        return
    if asset.platform == "mattermost":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mattermost asset is not linked to a channel")
        auth_config = _load_auth_config(asset.channel)
        token = auth_config.get_str("access_token")
        base_url = _normalize_server_url(auth_config.get_str("server_url"))
        if not token or not asset.provider_message_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mattermost asset is missing delete credentials")
        try:
            _perform_request(
                f"{base_url}/api/v4/posts/{parse.quote(asset.provider_message_id, safe='')}",
                method="DELETE",
                headers={"Authorization": f"Bearer {token}"},
            )
        except RuntimeError as exc:
            if str(exc).startswith("404 "):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mattermost post is already gone") from exc
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Mattermost delete failed: {exc}") from exc
        return
    if asset.platform == "discord":
        if not asset.channel:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Discord asset is not linked to a channel")
        if not asset.provider_message_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Discord asset is missing a message identifier")
        auth_config = _load_auth_config(asset.channel)
        target_config = _load_target_config(asset.channel)
        if asset.channel.auth_type == "bot":
            token = auth_config.get_str("bot_token")
            channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
            _perform_request(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{asset.provider_message_id}",
                method="DELETE",
                headers={"Authorization": f"Bot {token}"},
            )
            return
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Remote asset deletion is not implemented for {asset.platform}",
    )


def _decode_broadcast_attachments(
    attachments: list[schemas.BroadcastAttachment],
) -> list[dict[str, object]]:
    decoded: list[dict[str, object]] = []
    for attachment in attachments:
        if not attachment.content_base64:
            continue
        try:
            content = base64.b64decode(attachment.content_base64)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Attachment '{attachment.name}' is not valid base64 data",
            ) from exc
        decoded.append(
            {
                "name": attachment.name,
                "type": attachment.type,
                "size": attachment.size,
                "mime_type": attachment.mime_type or "application/octet-stream",
                "content": content,
                "mirror_group_id": attachment.mirror_group_id,
            }
        )
    return decoded


def _attachment_caption(title: str, content: str) -> str:
    return "\n\n".join(part for part in [title.strip(), content.strip()] if part).strip()


def _build_multipart_body(
    fields: dict[str, str],
    files: list[dict[str, object]] | None = None,
) -> tuple[bytes, str]:
    boundary = f"syncraft-{uuid4().hex}"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for file_part in files or []:
        field_name = str(file_part["field_name"])
        filename = str(file_part["filename"])
        content_type = str(file_part.get("content_type", "application/octet-stream"))
        content = file_part["content"]
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("Multipart file content must be bytes")
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def _test_slack_webhook(auth_config: schemas.ChannelConfig) -> tuple[schemas.LastTestStatus, str]:
    webhook_url = auth_config.get_str("webhook_url")
    if not webhook_url.startswith(("http://", "https://")):
        return "failed", "Webhook URL must start with http:// or https://"

    payload = json.dumps({"text": "Syncraft connection test"}).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text != "ok":
        return "failed", f"Slack webhook rejected the request: {body_text or 'empty response'}"
    return "success", "Slack webhook accepted the test message"


def _test_slack_bot(
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> tuple[schemas.LastTestStatus, str]:
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")

    auth_payload = _slack_api_request("auth.test", token, {})
    if not auth_payload.get("ok"):
        return "failed", _format_slack_error(auth_payload, "Slack auth.test failed")

    post_payload = _slack_api_request(
        "chat.postMessage",
        token,
        {
            "channel": channel_id,
            "text": "Syncraft connection test",
        },
        method="POST",
    )
    if not post_payload.get("ok"):
        return "failed", _format_slack_error(post_payload, "Slack chat.postMessage failed")

    channel_name = post_payload.get("channel", channel_id)
    return "success", f"Slack bot token is valid and can post to {channel_name}"


def _test_telegram_bot(
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> tuple[schemas.LastTestStatus, str]:
    token = auth_config.get_str("bot_token")
    chat_id = _require_string(target_config.values.get("chat_id"), "chat_id")
    base_url = f"https://api.telegram.org/bot{token}"

    me_payload = _telegram_api_request(f"{base_url}/getMe")
    if not me_payload.get("ok"):
        return "failed", "Telegram bot token validation failed"

    chat_payload = _telegram_api_request(
        f"{base_url}/getChat?{parse.urlencode({'chat_id': chat_id})}"
    )
    if not chat_payload.get("ok"):
        description = chat_payload.get("description", "Telegram chat lookup failed")
        return "failed", description

    username = me_payload.get("result", {}).get("username", "telegram-bot")
    title = chat_payload.get("result", {}).get("title") or chat_payload.get("result", {}).get("id", chat_id)
    return "success", f"Telegram bot @{username} can reach {title}"


def _test_discord_bot(
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> tuple[schemas.LastTestStatus, str]:
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    me_payload = _discord_api_request(
        "https://discord.com/api/v10/users/@me",
        token,
        method="GET",
    )
    channel_payload = _discord_api_request(
        f"https://discord.com/api/v10/channels/{channel_id}",
        token,
        method="GET",
    )
    username = str(me_payload.get("username", "discord-bot"))
    channel_name = str(channel_payload.get("name", channel_id))
    return "success", f"Discord bot {username} can access #{channel_name}"


def _send_slack_webhook_sample(
    auth_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> tuple[schemas.LastTestStatus, str]:
    webhook_url = auth_config.get_str("webhook_url")
    payload = json.dumps(_build_slack_message_payload(title, content, attachments)).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text != "ok":
        return "failed", f"Slack webhook rejected the sample message: {body_text or 'empty response'}"
    return "success", "Sample message sent to Slack webhook"


def _test_mattermost_webhook(auth_config: schemas.ChannelConfig) -> tuple[schemas.LastTestStatus, str]:
    webhook_url = auth_config.get_str("webhook_url")
    if not webhook_url.startswith(("http://", "https://")):
        return "failed", "Webhook URL must start with http:// or https://"

    payload = json.dumps({"text": "Syncraft connection test"}).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text != "ok":
        return "failed", f"Mattermost webhook rejected the request: {body_text or 'empty response'}"
    return "success", "Mattermost webhook accepted the test message"


def _send_mattermost_webhook_message(
    auth_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    webhook_url = auth_config.get_str("webhook_url")
    payload = json.dumps({"text": _build_plain_message(title, content, attachments) or "Syncraft message"}).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text != "ok":
        return "failed", f"Mattermost webhook rejected the message: {body_text or 'empty response'}", []
    return "success", "Message sent to Mattermost webhook", []


def _test_mattermost_bot(
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> tuple[schemas.LastTestStatus, str]:
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    token = auth_config.get_str("access_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    me_payload = _mattermost_api_request(base_url, "/api/v4/users/me", token, method="GET")
    channel_payload = _mattermost_api_request(
        base_url,
        f"/api/v4/channels/{parse.quote(channel_id, safe='')}",
        token,
        method="GET",
    )
    username = str(me_payload.get("username") or me_payload.get("id") or "mattermost-bot")
    channel_name = str(channel_payload.get("display_name") or channel_payload.get("name") or channel_id)
    return "success", f"Mattermost bot {username} can access {channel_name}"


def _send_mattermost_bot_message(
    channel: models.Channel | schemas.ChannelCreate,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
    decoded_attachments: list[dict[str, object]],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    token = auth_config.get_str("access_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    message_text = _build_plain_message(title, content, attachments) or "Syncraft message"
    file_ids: list[str] = []
    created_assets: list[schemas.AssetCreate] = []

    for attachment in decoded_attachments:
        upload_payload = _mattermost_upload_file(base_url, token, channel_id, attachment)
        file_infos = upload_payload.get("file_infos")
        if not isinstance(file_infos, list) or not file_infos:
            raise RuntimeError("Mattermost file upload returned no file metadata")
        file_info = file_infos[0]
        if not isinstance(file_info, dict):
            raise RuntimeError("Mattermost file upload returned an invalid file metadata payload")
        file_id = str(file_info.get("id", ""))
        if not file_id:
            raise RuntimeError("Mattermost file upload did not return a file ID")
        file_ids.append(file_id)
        created_assets.append(_build_mattermost_asset(channel, base_url, file_info, attachment))

    post_payload = {"channel_id": channel_id, "message": message_text}
    if file_ids:
        post_payload["file_ids"] = file_ids
    message_payload = _mattermost_api_request(
        base_url,
        "/api/v4/posts",
        token,
        method="POST",
        body=json.dumps(post_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    provider_message_id = str(message_payload.get("id", ""))
    if provider_message_id:
        for asset in created_assets:
            asset.provider_message_id = provider_message_id
    if decoded_attachments:
        if len(decoded_attachments) == 1:
            return "success", "Attachment sent through Mattermost bot", created_assets
        return "success", f"{len(decoded_attachments)} attachments sent through Mattermost bot", created_assets
    return "success", "Message sent through Mattermost bot", []


def _test_rocketchat_webhook(auth_config: schemas.ChannelConfig) -> tuple[schemas.LastTestStatus, str]:
    webhook_url = auth_config.get_str("webhook_url")
    if not webhook_url.startswith(("http://", "https://")):
        return "failed", "Webhook URL must start with http:// or https://"

    payload = json.dumps({"text": "Syncraft connection test"}).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text and body_text.lower() == "success":
        return "success", "Rocket.Chat webhook accepted the test message"
    try:
        payload_body = json.loads(body_text) if body_text else {}
    except json.JSONDecodeError:
        payload_body = {}
    if isinstance(payload_body, dict) and payload_body.get("success") is True:
        return "success", "Rocket.Chat webhook accepted the test message"
    return "failed", f"Rocket.Chat webhook rejected the request: {body_text or 'empty response'}"


def _test_rocketchat_bot(
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
) -> tuple[schemas.LastTestStatus, str]:
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    auth_token = auth_config.get_str("auth_token")
    user_id = auth_config.get_str("user_id")
    room_id = _require_string(target_config.values.get("room_id"), "room_id")
    me_payload = _rocketchat_api_request(
        base_url,
        f"/api/v1/users.info?{parse.urlencode({'userId': user_id})}",
        auth_token,
        user_id,
        method="GET",
    )
    room_payload = _rocketchat_api_request(
        base_url,
        f"/api/v1/rooms.info?{parse.urlencode({'roomId': room_id})}",
        auth_token,
        user_id,
        method="GET",
    )
    username = str(me_payload.get("user", {}).get("username") if isinstance(me_payload.get("user"), dict) else "" or user_id)
    room_info = room_payload.get("room")
    room_name = room_id
    if isinstance(room_info, dict):
        room_name = str(room_info.get("fname") or room_info.get("name") or room_info.get("_id") or room_id)
    return "success", f"Rocket.Chat user {username} can access {room_name}"


def _send_rocketchat_webhook_message(
    auth_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    webhook_url = auth_config.get_str("webhook_url")
    payload = json.dumps({"text": _build_plain_message(title, content, attachments) or "Syncraft message"}).encode("utf-8")
    response = _perform_json_request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    body_text = response.decode("utf-8").strip()
    if body_text and body_text.lower() == "success":
        return "success", "Message sent to Rocket.Chat webhook", []
    try:
        payload_body = json.loads(body_text) if body_text else {}
    except json.JSONDecodeError:
        payload_body = {}
    if isinstance(payload_body, dict) and payload_body.get("success") is True:
        return "success", "Message sent to Rocket.Chat webhook", []
    return "failed", f"Rocket.Chat webhook rejected the message: {body_text or 'empty response'}", []


def _send_rocketchat_bot_message(
    channel: models.Channel | schemas.ChannelCreate,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
    decoded_attachments: list[dict[str, object]],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    auth_token = auth_config.get_str("auth_token")
    user_id = auth_config.get_str("user_id")
    room_id = _require_string(target_config.values.get("room_id"), "room_id")
    message_text = _build_plain_message(title, content, attachments) or "Syncraft message"

    if not decoded_attachments:
        _rocketchat_api_request(
            base_url,
            "/api/v1/chat.postMessage",
            auth_token,
            user_id,
            method="POST",
            body=json.dumps({"roomId": room_id, "text": message_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return "success", "Message sent through Rocket.Chat bot", []

    created_assets: list[schemas.AssetCreate] = []
    for attachment in decoded_attachments:
        payload = _rocketchat_upload_file(base_url, auth_token, user_id, room_id, message_text, attachment)
        created_assets.extend(_build_rocketchat_assets(channel, base_url, payload, attachment))
        message_text = ""
    if len(decoded_attachments) == 1:
        return "success", "Attachment sent through Rocket.Chat bot", created_assets
    return "success", f"{len(decoded_attachments)} attachments sent through Rocket.Chat bot", created_assets


def _send_slack_bot_sample(
    channel: models.Channel | schemas.ChannelCreate,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    decoded_attachments = _decode_broadcast_attachments(attachments)
    message, assets = _send_slack_bot_message(channel, token, channel_id, title, content, attachments, decoded_attachments)
    return "success", message, assets


def _send_telegram_bot_sample(
    channel: models.Channel | schemas.ChannelCreate,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    token = auth_config.get_str("bot_token")
    chat_id = _require_string(target_config.values.get("chat_id"), "chat_id")
    decoded_attachments = _decode_broadcast_attachments(attachments)
    message, assets = _send_telegram_bot_message(channel, token, chat_id, title, content, attachments, decoded_attachments)
    return "success", message, assets


def _send_discord_bot_message(
    channel: models.Channel | schemas.ChannelCreate,
    auth_config: schemas.ChannelConfig,
    target_config: schemas.ChannelConfig,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
    decoded_attachments: list[dict[str, object]],
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    message_content = _build_plain_message(title, content, attachments) or "Syncraft message"
    if not decoded_attachments:
        response = _perform_request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            method="POST",
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"content": message_content}).encode("utf-8"),
        )
        try:
            payload = json.loads(response.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("message"):
            return "failed", str(payload["message"]), []
        remote_assets = _build_discord_assets(channel, payload, attachments)
        return "success", "Message sent through Discord bot", remote_assets
    payload_json = json.dumps({"content": message_content})
    files = [
        {
            "field_name": f"files[{index}]",
            "filename": str(item["name"]),
            "content_type": str(item["mime_type"]),
            "content": item["content"],
        }
        for index, item in enumerate(decoded_attachments)
    ]
    body, boundary = _build_multipart_body({"payload_json": payload_json}, files)
    response = _perform_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        body=body,
    )
    try:
        payload = json.loads(response.decode("utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload.get("message"):
        return "failed", str(payload["message"]), []
    remote_assets = _build_discord_assets(channel, payload, attachments)
    return "success", "Message sent through Discord bot", remote_assets


def _discord_api_request(
    url: str,
    token: str,
    *,
    method: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    merged_headers = {"Authorization": f"Bot {token}"}
    if headers:
        merged_headers.update(headers)
    response = _perform_json_request(url, method=method, headers=merged_headers, body=body)
    return json.loads(response.decode("utf-8"))


def _chunk_text(text: str, chunk_size: int = 1800) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    return [normalized[index:index + chunk_size] for index in range(0, len(normalized), chunk_size)]


def _build_plain_message(
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> str:
    parts: list[str] = []
    if title.strip():
        parts.append(title.strip())
    if content.strip():
        parts.append(content.strip())
    if attachments:
        parts.append(
            "Attachments:\n"
            + "\n".join(
                f"- {attachment.name} ({attachment.type}, {attachment.size} bytes)"
                for attachment in attachments
            )
        )
    return "\n\n".join(parts).strip()


def _build_slack_message_payload(
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
) -> dict[str, object]:
    fallback_text = _build_plain_message(title, content, attachments) or "Syncraft message"
    blocks: list[dict[str, object]] = []
    if title.strip():
        blocks.append(
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title.strip()[:150],
                },
            }
        )
    if content.strip():
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": content.strip(),
                },
            }
        )
    if attachments:
        attachment_text = "\n".join(
            f"• `{attachment.name}` ({attachment.type}, {attachment.size} bytes)"
            for attachment in attachments
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Attachments*\n{attachment_text}",
                },
            }
        )
    if not blocks:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": fallback_text}})
    return {"text": fallback_text, "blocks": blocks}


def _send_slack_bot_message(
    channel: models.Channel | schemas.ChannelCreate,
    token: str,
    channel_id: str,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
    decoded_attachments: list[dict[str, object]],
) -> tuple[str, list[schemas.AssetCreate]]:
    if decoded_attachments:
        message_text = _attachment_caption(title, content)
        created_assets: list[schemas.AssetCreate] = []
        for index, attachment in enumerate(decoded_attachments):
            get_url_payload = _slack_api_request(
                "files.getUploadURLExternal",
                token,
                {
                    "filename": str(attachment["name"]),
                    "length": str(len(attachment["content"])),
                },
                method="POST",
            )
            if not get_url_payload.get("ok"):
                raise RuntimeError(
                    _format_slack_error(get_url_payload, "Slack files.getUploadURLExternal failed")
                )

            upload_url = str(get_url_payload.get("upload_url", ""))
            file_id = str(get_url_payload.get("file_id", ""))
            if not upload_url or not file_id:
                raise RuntimeError("Slack file upload URL generation returned an incomplete response")

            _perform_request(
                upload_url,
                method="POST",
                headers={
                    "Content-Type": str(attachment["mime_type"]) or "application/octet-stream",
                },
                body=attachment["content"],
            )

            complete_payload = _slack_api_request(
                "files.completeUploadExternal",
                token,
                {
                    "files": json.dumps(
                        [
                            {
                                "id": file_id,
                                "title": str(attachment["name"]),
                            }
                        ]
                    ),
                    "channel_id": channel_id,
                    **(
                        {"initial_comment": message_text}
                        if index == 0 and message_text
                        else {}
                    ),
                },
                method="POST",
            )
            if not complete_payload.get("ok"):
                raise RuntimeError(
                    _format_slack_error(complete_payload, "Slack files.completeUploadExternal failed")
                )
            slack_files = complete_payload.get("files", [])
            if isinstance(slack_files, list) and slack_files:
                slack_file = slack_files[0]
                if isinstance(slack_file, dict):
                    created_assets.append(
                        _build_remote_asset_payload(
                            channel,
                            provider_asset_id=str(slack_file.get("id", "")),
                            remote_url=str(
                                slack_file.get("url_private_download")
                                or slack_file.get("url_private")
                                or ""
                            ),
                            preview_url=str(slack_file.get("thumb_360") or slack_file.get("url_private") or ""),
                            name=str(slack_file.get("name") or attachment["name"]),
                            mime_type=str(slack_file.get("mimetype") or attachment["mime_type"]),
                            size_bytes=int(slack_file.get("size") or len(attachment["content"])),
                            asset_type=str(attachment["type"]),
                            mirror_group_id=str(attachment.get("mirror_group_id", "")),
                        )
                    )
        if len(decoded_attachments) == 1:
            return "Attachment sent through Slack bot", created_assets
        return f"{len(decoded_attachments)} attachments sent through Slack bot", created_assets

    slack_payload = _build_slack_message_payload(title, content, attachments)
    payload = _slack_api_request(
        "chat.postMessage",
        token,
        {
            "channel": channel_id,
            "text": str(slack_payload["text"]),
            "blocks": json.dumps(slack_payload["blocks"]),
        },
        method="POST",
    )
    if not payload.get("ok"):
        raise RuntimeError(_format_slack_error(payload, "Slack chat.postMessage failed"))
    return "Sample message sent through Slack bot", []


def _send_telegram_bot_message(
    channel: models.Channel | schemas.ChannelCreate,
    token: str,
    chat_id: str,
    title: str,
    content: str,
    attachments: list[schemas.BroadcastAttachment],
    decoded_attachments: list[dict[str, object]],
) -> tuple[str, list[schemas.AssetCreate]]:
    caption = _attachment_caption(title, content)
    if not decoded_attachments:
        payload = _telegram_api_request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            body=parse.urlencode({"chat_id": chat_id, "text": _build_plain_message(title, content, attachments)}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description", "Telegram sendMessage failed")))
        return "Sample message sent through Telegram bot", []

    if caption and len(decoded_attachments) > 1:
        payload = _telegram_api_request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            body=parse.urlencode({"chat_id": chat_id, "text": caption}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description", "Telegram sendMessage failed")))

    created_assets: list[schemas.AssetCreate] = []
    for index, attachment in enumerate(decoded_attachments):
        endpoint, field_name = _telegram_endpoint_for_attachment(str(attachment["mime_type"]), str(attachment["name"]))
        fields = {"chat_id": chat_id}
        if caption and len(decoded_attachments) == 1 and index == 0:
            fields["caption"] = caption
        body, boundary = _build_multipart_body(
            fields,
            [
                {
                    "field_name": field_name,
                    "filename": str(attachment["name"]),
                    "content_type": str(attachment["mime_type"]),
                    "content": attachment["content"],
                }
            ],
        )
        payload = _telegram_api_request(
            f"https://api.telegram.org/bot{token}/{endpoint}",
            method="POST",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description", f"Telegram {endpoint} failed")))
        created_assets.extend(_build_telegram_assets(channel, payload, attachment))

    if len(decoded_attachments) == 1:
        return "Attachment sent through Telegram bot", created_assets
    return f"{len(decoded_attachments)} attachments sent through Telegram bot", created_assets


def _telegram_endpoint_for_attachment(mime_type: str, filename: str) -> tuple[str, str]:
    lower_name = filename.lower()
    lower_type = mime_type.lower()
    if lower_type.startswith("image/"):
        return "sendPhoto", "photo"
    if lower_type.startswith("video/"):
        return "sendVideo", "video"
    if lower_name.endswith((".mp3", ".wav", ".m4a", ".ogg")) or lower_type.startswith("audio/"):
        return "sendAudio", "audio"
    return "sendDocument", "document"

def _build_telegram_assets(
    channel: models.Channel | schemas.ChannelCreate,
    payload: dict[str, object],
    attachment: dict[str, object],
) -> list[schemas.AssetCreate]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    telegram_file: dict[str, object] | None = None
    if isinstance(result.get("document"), dict):
        telegram_file = result["document"]
    elif isinstance(result.get("video"), dict):
        telegram_file = result["video"]
    elif isinstance(result.get("audio"), dict):
        telegram_file = result["audio"]
    elif isinstance(result.get("photo"), list) and result["photo"]:
        last_photo = result["photo"][-1]
        if isinstance(last_photo, dict):
            telegram_file = last_photo
    if telegram_file is None:
        return []
    return [
        _build_remote_asset_payload(
            channel,
            provider_asset_id=str(telegram_file.get("file_id", "")),
            provider_message_id=str(result.get("message_id", "")),
            remote_url="",
            preview_url="",
            name=str(attachment["name"]),
            mime_type=str(attachment["mime_type"]),
            size_bytes=int(telegram_file.get("file_size") or attachment["size"]),
            asset_type=str(attachment["type"]),
            mirror_group_id=str(attachment.get("mirror_group_id", "")),
        )
    ]


def _build_discord_assets(
    channel: models.Channel | schemas.ChannelCreate,
    payload: dict[str, object],
    attachments: list[schemas.BroadcastAttachment],
) -> list[schemas.AssetCreate]:
    discord_attachments = payload.get("attachments")
    if not isinstance(discord_attachments, list):
        return []
    created_assets: list[schemas.AssetCreate] = []
    for index, discord_attachment in enumerate(discord_attachments):
        if not isinstance(discord_attachment, dict):
            continue
        source_attachment = attachments[index] if index < len(attachments) else None
        fallback_name = source_attachment.name if source_attachment else str(discord_attachment.get("filename", "attachment"))
        fallback_type = source_attachment.type if source_attachment else "document"
        created_assets.append(
            _build_remote_asset_payload(
                channel,
                provider_asset_id=str(discord_attachment.get("id", "")),
                provider_message_id=str(payload.get("id", "")),
                remote_url=str(discord_attachment.get("url", "")),
                preview_url=str(discord_attachment.get("proxy_url", "") or discord_attachment.get("url", "")),
                name=str(discord_attachment.get("filename", fallback_name)),
                mime_type=str(discord_attachment.get("content_type") or (source_attachment.mime_type if source_attachment else "application/octet-stream")),
                size_bytes=int(discord_attachment.get("size") or (source_attachment.size if source_attachment else 0)),
                asset_type=fallback_type,
                mirror_group_id=source_attachment.mirror_group_id if source_attachment else "",
            )
        )
    return created_assets


def _sync_slack_channel_messages(channel: models.Channel) -> list[schemas.ChannelMessageCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    params = {"channel": channel_id, "limit": "100"}
    if channel.message_sync_cursor:
        params["oldest"] = channel.message_sync_cursor
        params["inclusive"] = "false"
    payload = _slack_api_request("conversations.history", token, params)
    if not payload.get("ok"):
        raise RuntimeError(_format_slack_error(payload, "Slack conversations.history failed"))
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    created_messages: list[schemas.ChannelMessageCreate] = []
    max_ts = channel.message_sync_cursor
    for item in messages:
        if not isinstance(item, dict):
            continue
        provider_message_id = str(item.get("ts", "")).strip()
        if not provider_message_id:
            continue
        content = str(item.get("text") or "")
        files = item.get("files")
        attachment_count = len(files) if isinstance(files, list) else 0
        if not content.strip() and attachment_count == 0:
            continue
        author_name = ""
        bot_profile = item.get("bot_profile")
        if isinstance(bot_profile, dict):
            author_name = str(bot_profile.get("name") or "")
        if not author_name:
            author_name = str(item.get("username") or item.get("user") or "")
        created_messages.append(
            _build_channel_message_payload(
                channel,
                provider_chat_id=channel_id,
                provider_message_id=provider_message_id,
                content=content,
                sent_at=_datetime_from_unix_timestamp(provider_message_id),
                author_name=author_name,
                attachment_count=attachment_count,
            )
        )
        if not max_ts or provider_message_id > max_ts:
            max_ts = provider_message_id
    channel.message_sync_cursor = max_ts
    return created_messages


def _sync_discord_channel_messages(channel: models.Channel) -> list[schemas.ChannelMessageCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    query_params = {"limit": "100"}
    if channel.message_sync_cursor:
        query_params["after"] = channel.message_sync_cursor
    response = _perform_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages?{parse.urlencode(query_params)}",
        method="GET",
        headers={"Authorization": f"Bot {token}"},
    )
    payload = json.loads(response.decode("utf-8"))
    if isinstance(payload, dict) and payload.get("message"):
        raise RuntimeError(str(payload["message"]))
    if not isinstance(payload, list):
        return []

    created_messages: list[schemas.ChannelMessageCreate] = []
    max_message_id = channel.message_sync_cursor
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider_message_id = str(item.get("id", "")).strip()
        if not provider_message_id:
            continue
        attachments = item.get("attachments")
        attachment_count = len(attachments) if isinstance(attachments, list) else 0
        content = str(item.get("content") or "")
        if not content.strip() and attachment_count == 0:
            continue
        author = item.get("author")
        author_name = str(author.get("username") or "") if isinstance(author, dict) else ""
        created_messages.append(
            _build_channel_message_payload(
                channel,
                provider_chat_id=channel_id,
                provider_message_id=provider_message_id,
                content=content,
                sent_at=_discord_timestamp_from_snowflake(provider_message_id),
                author_name=author_name,
                attachment_count=attachment_count,
            )
        )
        if not max_message_id or provider_message_id > max_message_id:
            max_message_id = provider_message_id
    channel.message_sync_cursor = max_message_id
    return created_messages


def _sync_mattermost_channel_messages(channel: models.Channel) -> list[schemas.ChannelMessageCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("access_token")
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    params = {"page": "0", "per_page": "100"}
    if channel.message_sync_cursor:
        params["since"] = channel.message_sync_cursor
    payload = _mattermost_api_request(
        base_url,
        f"/api/v4/channels/{parse.quote(channel_id, safe='')}/posts?{parse.urlencode(params)}",
        token,
        method="GET",
    )
    order = payload.get("order")
    posts = payload.get("posts")
    if not isinstance(order, list) or not isinstance(posts, dict):
        return []

    created_messages: list[schemas.ChannelMessageCreate] = []
    max_timestamp = channel.message_sync_cursor
    for post_id in order:
        post = posts.get(post_id) if isinstance(post_id, str) else None
        if not isinstance(post, dict):
            continue
        provider_message_id = str(post.get("id", "")).strip()
        if not provider_message_id:
            continue
        content = str(post.get("message") or "")
        file_ids = post.get("file_ids")
        attachment_count = len(file_ids) if isinstance(file_ids, list) else 0
        if not content.strip() and attachment_count == 0:
            continue
        sent_at = _datetime_from_milliseconds(post.get("create_at"))
        created_messages.append(
            _build_channel_message_payload(
                channel,
                provider_chat_id=channel_id,
                provider_message_id=provider_message_id,
                content=content,
                sent_at=sent_at,
                author_name=str(post.get("user_id") or ""),
                attachment_count=attachment_count,
            )
        )
        update_raw = str(post.get("update_at") or post.get("create_at") or "").strip()
        if update_raw and (not max_timestamp or update_raw > max_timestamp):
            max_timestamp = update_raw
    channel.message_sync_cursor = max_timestamp
    return created_messages


def _sync_rocketchat_channel_messages(channel: models.Channel) -> list[schemas.ChannelMessageCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    auth_token = auth_config.get_str("auth_token")
    user_id = auth_config.get_str("user_id")
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    room_id = _require_string(target_config.values.get("room_id"), "room_id")
    room_info_payload = _rocketchat_api_request(
        base_url,
        f"/api/v1/rooms.info?{parse.urlencode({'roomId': room_id})}",
        auth_token,
        user_id,
        method="GET",
    )
    room = room_info_payload.get("room")
    room_type = str(room.get("t") or "") if isinstance(room, dict) else ""
    history_path = "/api/v1/channels.history"
    room_param_name = "roomId"
    if room_type == "p":
        history_path = "/api/v1/groups.history"
    elif room_type == "d":
        history_path = "/api/v1/im.history"

    params = {room_param_name: room_id, "count": "100"}
    if channel.message_sync_cursor:
        params["oldest"] = channel.message_sync_cursor
    payload = _rocketchat_api_request(
        base_url,
        f"{history_path}?{parse.urlencode(params)}",
        auth_token,
        user_id,
        method="GET",
    )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    created_messages: list[schemas.ChannelMessageCreate] = []
    max_timestamp = channel.message_sync_cursor
    for item in messages:
        if not isinstance(item, dict):
            continue
        provider_message_id = str(item.get("_id", "")).strip()
        sent_at = _datetime_from_iso_timestamp(item.get("ts"))
        if not provider_message_id or sent_at is None:
            continue
        content = str(item.get("msg") or "")
        files = item.get("files")
        attachments = item.get("attachments")
        attachment_count = 0
        if isinstance(files, list):
            attachment_count += len(files)
        if isinstance(attachments, list):
            attachment_count += len(attachments)
        if not content.strip() and attachment_count == 0:
            continue
        user = item.get("u")
        author_name = str(user.get("username") or user.get("name") or "") if isinstance(user, dict) else ""
        sent_at_text = sent_at.isoformat()
        created_messages.append(
            _build_channel_message_payload(
                channel,
                provider_chat_id=room_id,
                provider_message_id=provider_message_id,
                content=content,
                sent_at=sent_at,
                author_name=author_name,
                attachment_count=attachment_count,
            )
        )
        if not max_timestamp or sent_at_text > max_timestamp:
            max_timestamp = sent_at_text
    channel.message_sync_cursor = max_timestamp
    return created_messages


def _sync_telegram_bot_messages(
    token: str,
    *,
    offset: int = 0,
) -> dict[str, object]:
    params: dict[str, str] = {
        "timeout": "0",
        "allowed_updates": json.dumps(["message", "edited_message", "channel_post", "edited_channel_post"]),
    }
    if offset > 0:
        params["offset"] = str(offset)
    payload = _telegram_api_request(
        f"https://api.telegram.org/bot{token}/getUpdates?{parse.urlencode(params)}"
    )
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description", "Telegram getUpdates failed")))
    result = payload.get("result")
    if not isinstance(result, list):
        return {"messages": [], "next_offset": offset}

    messages: list[dict[str, object]] = []
    next_offset = offset
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        if update_id > 0:
            next_offset = max(next_offset, update_id + 1)
        message_payload = None
        for key in ("channel_post", "edited_channel_post", "message", "edited_message"):
            candidate = update.get(key)
            if isinstance(candidate, dict):
                message_payload = candidate
                break
        if message_payload is None:
            continue
        messages.append(message_payload)
    return {"messages": messages, "next_offset": next_offset}


def _sync_telegram_bot_assets(channel: models.Channel) -> list[schemas.AssetCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("bot_token")
    chat_id = _require_string(target_config.values.get("chat_id"), "chat_id")
    offset = int(channel.asset_sync_cursor) if channel.asset_sync_cursor.strip().isdigit() else 0
    payload = _sync_telegram_bot_messages(token, offset=offset)
    created_assets: list[schemas.AssetCreate] = []
    for message_payload in payload["messages"]:
        provider_chat_id = str(message_payload.get("chat", {}).get("id", ""))
        if provider_chat_id != chat_id:
            continue
        asset_payload = _build_telegram_asset_from_message(channel, message_payload)
        if asset_payload is not None:
            created_assets.append(asset_payload)
    if payload["next_offset"]:
        channel.asset_sync_cursor = str(payload["next_offset"])
    return created_assets


def _sync_slack_channel_assets(channel: models.Channel) -> list[schemas.AssetCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    created_assets: list[schemas.AssetCreate] = []
    max_created = channel.asset_sync_cursor
    page = 1
    while True:
        params: dict[str, str] = {"channel": channel_id, "count": "100", "page": str(page)}
        payload = _slack_api_request("files.list", token, params)
        if not payload.get("ok"):
            raise RuntimeError(_format_slack_error(payload, "Slack files.list failed"))
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            break
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            created_raw = file_item.get("created")
            created_text = str(created_raw) if created_raw is not None else ""
            if created_text and (not max_created or created_text > max_created):
                max_created = created_text
            created_assets.append(
                _build_remote_asset_payload(
                    channel,
                    provider_asset_id=str(file_item.get("id", "")),
                    remote_url=str(file_item.get("url_private_download") or file_item.get("url_private") or ""),
                    preview_url=str(file_item.get("thumb_360") or file_item.get("url_private") or ""),
                    name=str(file_item.get("name", "slack-file")),
                    mime_type=str(file_item.get("mimetype") or "application/octet-stream"),
                    size_bytes=int(file_item.get("size") or 0),
                    asset_type=str(file_item.get("filetype") or "document"),
                )
            )
        paging = payload.get("paging")
        pages = int(paging.get("pages") or page) if isinstance(paging, dict) else page
        if page >= pages:
            break
        page += 1
    channel.asset_sync_cursor = max_created
    return created_assets


def _sync_discord_channel_assets(channel: models.Channel) -> list[schemas.AssetCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("bot_token")
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    created_assets: list[schemas.AssetCreate] = []
    max_message_id = channel.asset_sync_cursor
    before_message_id = ""
    while True:
        query_params = {"limit": "100"}
        if before_message_id:
            query_params["before"] = before_message_id
        response = _perform_request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages?{parse.urlencode(query_params)}",
            method="GET",
            headers={"Authorization": f"Bot {token}"},
        )
        payload = json.loads(response.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("message"):
            raise RuntimeError(str(payload["message"]))
        if not isinstance(payload, list) or not payload:
            break
        for message in payload:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("id", ""))
            if message_id and (not max_message_id or message_id > max_message_id):
                max_message_id = message_id
            attachments = message.get("attachments")
            if not isinstance(attachments, list):
                continue
            normalized_attachments = [
                schemas.BroadcastAttachment(
                    name=str(attachment.get("filename", "attachment")),
                    type=_normalize_asset_type(
                        str(attachment.get("content_type") or "document"),
                        str(attachment.get("content_type") or "application/octet-stream"),
                    ),
                    size=int(attachment.get("size") or 0),
                    mime_type=str(attachment.get("content_type") or "application/octet-stream"),
                )
                for attachment in attachments
                if isinstance(attachment, dict)
            ]
            created_assets.extend(_build_discord_assets(channel, message, normalized_attachments))
        last_message = payload[-1] if payload else None
        before_message_id = str(last_message.get("id", "")) if isinstance(last_message, dict) else ""
        if len(payload) < 100 or not before_message_id:
            break
    channel.asset_sync_cursor = max_message_id
    return created_assets


def _build_mattermost_asset_from_file_info(
    channel: models.Channel | schemas.ChannelCreate,
    base_url: str,
    post_id: str,
    file_info: dict[str, object],
) -> schemas.AssetCreate | None:
    file_id = str(file_info.get("id", "")).strip()
    if not file_id:
        return None
    file_name = str(file_info.get("name") or file_info.get("filename") or "mattermost-file")
    mime_type = str(file_info.get("mime_type") or "application/octet-stream")
    remote_url = f"{base_url}/api/v4/files/{file_id}"
    preview_path = str(file_info.get("mini_preview") or "")
    preview_url = preview_path if preview_path.startswith(("http://", "https://")) else (f"{base_url}{preview_path}" if preview_path else remote_url)
    return _build_remote_asset_payload(
        channel,
        provider_asset_id=file_id,
        provider_message_id=post_id,
        remote_url=remote_url,
        preview_url=preview_url,
        name=file_name,
        mime_type=mime_type,
        size_bytes=int(file_info.get("size") or 0),
        asset_type=str(file_info.get("extension") or "document"),
    )


def _sync_mattermost_channel_assets(channel: models.Channel) -> list[schemas.AssetCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    token = auth_config.get_str("access_token")
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    channel_id = _require_string(target_config.values.get("channel_id"), "channel_id")
    params = {"page": "0", "per_page": "100"}
    if channel.asset_sync_cursor:
        params["since"] = channel.asset_sync_cursor
    payload = _mattermost_api_request(
        base_url,
        f"/api/v4/channels/{parse.quote(channel_id, safe='')}/posts?{parse.urlencode(params)}",
        token,
        method="GET",
    )
    order = payload.get("order")
    posts = payload.get("posts")
    if not isinstance(order, list) or not isinstance(posts, dict):
        return []

    created_assets: list[schemas.AssetCreate] = []
    max_timestamp = channel.asset_sync_cursor
    for post_id in order:
        post = posts.get(post_id) if isinstance(post_id, str) else None
        if not isinstance(post, dict):
            continue
        provider_message_id = str(post.get("id", "")).strip()
        if not provider_message_id:
            continue
        update_raw = str(post.get("update_at") or post.get("create_at") or "").strip()
        if update_raw and (not max_timestamp or update_raw > max_timestamp):
            max_timestamp = update_raw
        file_ids = post.get("file_ids")
        if not isinstance(file_ids, list):
            continue
        for file_id in file_ids:
            if not isinstance(file_id, str) or not file_id.strip():
                continue
            file_info = _mattermost_api_request(
                base_url,
                f"/api/v4/files/{parse.quote(file_id, safe='')}/info",
                token,
                method="GET",
            )
            asset_payload = _build_mattermost_asset_from_file_info(channel, base_url, provider_message_id, file_info)
            if asset_payload is not None:
                created_assets.append(asset_payload)
    channel.asset_sync_cursor = max_timestamp
    return created_assets


def _sync_rocketchat_channel_assets(channel: models.Channel) -> list[schemas.AssetCreate]:
    auth_config = _load_auth_config(channel)
    target_config = _load_target_config(channel)
    auth_token = auth_config.get_str("auth_token")
    user_id = auth_config.get_str("user_id")
    base_url = _normalize_server_url(auth_config.get_str("server_url"))
    room_id = _require_string(target_config.values.get("room_id"), "room_id")
    room_info_payload = _rocketchat_api_request(
        base_url,
        f"/api/v1/rooms.info?{parse.urlencode({'roomId': room_id})}",
        auth_token,
        user_id,
        method="GET",
    )
    room = room_info_payload.get("room")
    room_type = str(room.get("t") or "") if isinstance(room, dict) else ""
    history_path = "/api/v1/channels.history"
    if room_type == "p":
        history_path = "/api/v1/groups.history"
    elif room_type == "d":
        history_path = "/api/v1/im.history"

    params = {"roomId": room_id, "count": "100"}
    if channel.asset_sync_cursor:
        params["oldest"] = channel.asset_sync_cursor
    payload = _rocketchat_api_request(
        base_url,
        f"{history_path}?{parse.urlencode(params)}",
        auth_token,
        user_id,
        method="GET",
    )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    created_assets: list[schemas.AssetCreate] = []
    max_timestamp = channel.asset_sync_cursor
    for message in messages:
        if not isinstance(message, dict):
            continue
        provider_message_id = str(message.get("_id", "")).strip()
        sent_at = _datetime_from_iso_timestamp(message.get("ts"))
        if not provider_message_id or sent_at is None:
            continue
        sent_at_text = sent_at.isoformat()
        if not max_timestamp or sent_at_text > max_timestamp:
            max_timestamp = sent_at_text
        files = message.get("files")
        attachments = message.get("attachments")
        if not isinstance(files, list):
            continue
        attachment_items = attachments if isinstance(attachments, list) else []
        for index, file_info in enumerate(files):
            if not isinstance(file_info, dict):
                continue
            provider_asset_id = str(file_info.get("_id") or "").strip()
            if not provider_asset_id:
                continue
            attachment_info = (
                attachment_items[index]
                if index < len(attachment_items) and isinstance(attachment_items[index], dict)
                else {}
            )
            remote_path = str(attachment_info.get("title_link") or attachment_info.get("image_url") or "")
            remote_url = remote_path if remote_path.startswith(("http://", "https://")) else (f"{base_url}{remote_path}" if remote_path else "")
            preview_path = str(attachment_info.get("image_url") or attachment_info.get("title_link") or "")
            preview_url = preview_path if preview_path.startswith(("http://", "https://")) else (f"{base_url}{preview_path}" if preview_path else remote_url)
            mime_type = str(file_info.get("type") or "application/octet-stream")
            created_assets.append(
                _build_remote_asset_payload(
                    channel,
                    provider_asset_id=provider_asset_id,
                    provider_message_id=provider_message_id,
                    remote_url=remote_url,
                    preview_url=preview_url,
                    name=str(file_info.get("name") or "rocketchat-file"),
                    mime_type=mime_type,
                    size_bytes=int(attachment_info.get("image_size") or 0),
                    asset_type=mime_type,
                )
            )
    channel.asset_sync_cursor = max_timestamp
    return created_assets


def _slack_api_request(
    endpoint: str,
    token: str,
    params: dict[str, str],
    *,
    method: str = "GET",
) -> dict[str, object]:
    url = f"https://slack.com/api/{endpoint}"
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if method == "GET" and params:
        url = f"{url}?{parse.urlencode(params)}"
    elif method != "GET":
        body = parse.urlencode(params).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = _perform_json_request(
        url,
        method=method,
        headers=headers,
        body=body,
    )
    return json.loads(response.decode("utf-8"))


def _normalize_server_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Server URL must start with http:// or https://",
        )
    return normalized


def _mattermost_api_request(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    response = _perform_json_request(
        f"{base_url}{path}",
        method=method,
        headers=request_headers,
        body=body,
    )
    return json.loads(response.decode("utf-8"))


def _mattermost_upload_file(
    base_url: str,
    token: str,
    channel_id: str,
    attachment: dict[str, object],
) -> dict[str, object]:
    body, boundary = _build_multipart_body(
        {"channel_id": channel_id},
        [
            {
                "field_name": "files",
                "filename": str(attachment["name"]),
                "content_type": str(attachment["mime_type"]),
                "content": attachment["content"],
            }
        ],
    )
    return _mattermost_api_request(
        base_url,
        "/api/v4/files",
        token,
        method="POST",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def _build_mattermost_asset(
    channel: models.Channel | schemas.ChannelCreate,
    base_url: str,
    file_info: dict[str, object],
    attachment: dict[str, object],
) -> schemas.AssetCreate:
    file_id = str(file_info.get("id", ""))
    file_name = str(file_info.get("name") or attachment["name"])
    remote_url = f"{base_url}/api/v4/files/{file_id}" if file_id else ""
    preview_path = str(file_info.get("mini_preview") or "")
    preview_url = preview_path if preview_path.startswith(("http://", "https://")) else (f"{base_url}{preview_path}" if preview_path else remote_url)
    return _build_remote_asset_payload(
        channel,
        provider_asset_id=file_id,
        remote_url=remote_url,
        preview_url=preview_url,
        name=file_name,
        mime_type=str(file_info.get("mime_type") or attachment["mime_type"]),
        size_bytes=int(file_info.get("size") or len(attachment["content"])),
        asset_type=str(attachment["type"]),
        mirror_group_id=str(attachment.get("mirror_group_id", "")),
    )


def _rocketchat_api_request(
    base_url: str,
    path: str,
    auth_token: str,
    user_id: str,
    *,
    method: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request_headers = {
        "X-Auth-Token": auth_token,
        "X-User-Id": user_id,
    }
    if headers:
        request_headers.update(headers)
    response = _perform_json_request(
        f"{base_url}{path}",
        method=method,
        headers=request_headers,
        body=body,
    )
    payload = json.loads(response.decode("utf-8"))
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(str(payload.get("error") or payload.get("message") or "Rocket.Chat request failed"))
    return payload


def _rocketchat_upload_file(
    base_url: str,
    auth_token: str,
    user_id: str,
    room_id: str,
    message_text: str,
    attachment: dict[str, object],
) -> dict[str, object]:
    upload_body, upload_boundary = _build_multipart_body(
        {},
        [
            {
                "field_name": "file",
                "filename": str(attachment["name"]),
                "content_type": str(attachment["mime_type"]),
                "content": attachment["content"],
            }
        ],
    )
    upload_payload = _rocketchat_api_request(
        base_url,
        f"/api/v1/rooms.media/{parse.quote(room_id, safe='')}",
        auth_token,
        user_id,
        method="POST",
        body=upload_body,
        headers={"Content-Type": f"multipart/form-data; boundary={upload_boundary}"},
    )
    file_payload = upload_payload.get("file")
    if not isinstance(file_payload, dict):
            raise RuntimeError("Rocket.Chat upload did not return file metadata")
    file_id = str(file_payload.get("_id", ""))
    if not file_id:
            raise RuntimeError("Rocket.Chat upload did not return a file ID")

    confirm_payload: dict[str, object] = {}
    if message_text:
        confirm_payload["msg"] = message_text
        confirm_payload["description"] = message_text[:240]
    return _rocketchat_api_request(
        base_url,
        f"/api/v1/rooms.mediaConfirm/{parse.quote(room_id, safe='')}/{parse.quote(file_id, safe='')}",
        auth_token,
        user_id,
        method="POST",
        body=json.dumps(confirm_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _build_rocketchat_assets(
    channel: models.Channel | schemas.ChannelCreate,
    base_url: str,
    payload: dict[str, object],
    attachment: dict[str, object],
) -> list[schemas.AssetCreate]:
    message_payload = payload.get("message")
    if isinstance(message_payload, dict):
        file_info = message_payload.get("file")
        attachments_payload = message_payload.get("attachments")
        provider_message_id = str(message_payload.get("_id", ""))
    else:
        file_info = payload.get("file")
        attachments_payload = payload.get("attachments")
        provider_message_id = str(payload.get("_id", ""))
    if not isinstance(file_info, dict):
        return []
    remote_url = ""
    preview_url = ""
    if isinstance(attachments_payload, list) and attachments_payload:
        attachment_payload = attachments_payload[0]
        if isinstance(attachment_payload, dict):
            remote_path = str(
                attachment_payload.get("title_link")
                or attachment_payload.get("image_url")
                or ""
            )
            if remote_path:
                remote_url = remote_path if remote_path.startswith(("http://", "https://")) else f"{base_url}{remote_path}"
            preview_path = str(
                attachment_payload.get("image_url")
                or attachment_payload.get("title_link")
                or ""
            )
            if preview_path:
                preview_url = preview_path if preview_path.startswith(("http://", "https://")) else f"{base_url}{preview_path}"
    return [
        _build_remote_asset_payload(
            channel,
            provider_asset_id=str(file_info.get("_id", "")),
            provider_message_id=provider_message_id,
            remote_url=remote_url,
            preview_url=preview_url or remote_url,
            name=str(file_info.get("name") or attachment["name"]),
            mime_type=str(file_info.get("type") or attachment["mime_type"]),
            size_bytes=int(
                (attachments_payload[0].get("image_size") if isinstance(attachments_payload, list) and attachments_payload and isinstance(attachments_payload[0], dict) else 0)
                or len(attachment["content"])
            ),
            asset_type=str(attachment["type"]),
            mirror_group_id=str(attachment.get("mirror_group_id", "")),
        )
    ]


def _format_slack_error(payload: dict[str, object], fallback: str) -> str:
    error_message = str(payload.get("error", fallback))
    needed_scope = payload.get("needed")
    provided_scope = payload.get("provided")
    if error_message == "missing_scope":
        details: list[str] = []
        if isinstance(needed_scope, str) and needed_scope.strip():
            details.append(f"needed: {needed_scope}")
        if isinstance(provided_scope, str) and provided_scope.strip():
            details.append(f"provided: {provided_scope}")
        if details:
            return f"Slack missing scope ({'; '.join(details)})"
        return "Slack missing required scope"
    return error_message


def _telegram_api_request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    response = _perform_json_request(url, method=method, body=body, headers=headers)
    return json.loads(response.decode("utf-8"))


def _perform_json_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float | None = None,
) -> bytes:
    return _perform_request(url, method=method, headers=headers, body=body, timeout=timeout)


def _perform_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float | None = None,
) -> bytes:
    request_headers = {
        "User-Agent": "Syncraft/1.0",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        request_headers.update(headers)
    req = request.Request(url, data=body, headers=request_headers, method=method)
    request_timeout = timeout
    if request_timeout is None:
        request_timeout = (
            PROVIDER_UPLOAD_TIMEOUT_SECONDS
            if body is not None and len(body) >= UPLOAD_TIMEOUT_BODY_THRESHOLD_BYTES
            else PROVIDER_TIMEOUT_SECONDS
        )
    urlopen_kwargs: dict[str, object] = {"timeout": request_timeout}
    ssl_context = _provider_ssl_context(url)
    if ssl_context is not None:
        urlopen_kwargs["context"] = ssl_context
    try:
        with request.urlopen(req, **urlopen_kwargs) as response:
            return response.read()
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="ignore").strip()
        reason = payload or exc.reason
        raise RuntimeError(f"{exc.code} {reason}") from exc
    except (error.URLError, ssl.SSLError, socket.timeout, TimeoutError) as exc:
        raise ProviderConnectivityError(_provider_connectivity_message(exc)) from exc


def _raise_missing_remote_asset(asset: models.Asset, exc: RuntimeError) -> None:
    message = str(exc)
    if message.startswith("404 "):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Remote asset is no longer available on {asset.platform}",
        ) from exc
    raise exc


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required provider setup fields: {field_name}",
        )
    return value.strip()


def _channel_message_ordering():
    return (
        models.ChannelMessage.sent_at.is_(None).asc(),
        models.ChannelMessage.sent_at.desc(),
        models.ChannelMessage.created_at.desc(),
    )


def _model_to_dict(model: object, **kwargs) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    if hasattr(model, "dict"):
        return model.dict(**kwargs)
    raise TypeError(f"Unsupported model type: {type(model)!r}")


def _model_to_json(model: object) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    if hasattr(model, "json"):
        return model.json()
    return json.dumps(_model_to_dict(model))


def _coerce_channel_config(value: object) -> schemas.ChannelConfig | None:
    if value is None:
        return None
    if isinstance(value, schemas.ChannelConfig):
        return value
    if isinstance(value, dict):
        return schemas.ChannelConfig(**value)
    raise TypeError(f"Unsupported channel config type: {type(value)!r}")


def send_channel_sample(
    channel: models.Channel | schemas.ChannelCreate,
    sample_message: str,
) -> tuple[schemas.LastTestStatus, str]:
    result, message, _ = send_channel_message(channel, content=sample_message)
    return result, message


def send_channel_message(
    channel: models.Channel | schemas.ChannelCreate,
    *,
    title: str = "",
    content: str = "",
    attachments: list[schemas.BroadcastAttachment] | None = None,
) -> tuple[schemas.LastTestStatus, str, list[schemas.AssetCreate]]:
    auth_config, target_config, platform, auth_type = _resolve_channel_test_payload(channel)
    validate_channel_payload(platform, auth_type, auth_config, target_config)
    attachments = attachments or []
    decoded_attachments = _decode_broadcast_attachments(attachments)
    try:
        if platform == "slack" and auth_type == "webhook":
            if decoded_attachments:
                return "failed", "Slack incoming webhooks do not support file uploads in Syncraft; use Slack bot auth for attachments", []
            result, message = _send_slack_webhook_sample(auth_config, title, content, attachments)
            return result, message, []
        if platform == "slack" and auth_type == "bot":
            return _send_slack_bot_sample(channel, auth_config, target_config, title, content, attachments)
        if platform == "mattermost" and auth_type == "webhook":
            if decoded_attachments:
                return "failed", "Mattermost incoming webhooks do not support file uploads in Syncraft; use Mattermost bot auth for attachments", []
            return _send_mattermost_webhook_message(auth_config, title, content, attachments)
        if platform == "mattermost" and auth_type == "bot":
            return _send_mattermost_bot_message(channel, auth_config, target_config, title, content, attachments, decoded_attachments)
        if platform == "rocketchat" and auth_type == "webhook":
            if decoded_attachments:
                return "failed", "Rocket.Chat incoming integrations do not support file uploads in Syncraft; use Rocket.Chat bot auth for attachments", []
            return _send_rocketchat_webhook_message(auth_config, title, content, attachments)
        if platform == "rocketchat" and auth_type == "bot":
            return _send_rocketchat_bot_message(channel, auth_config, target_config, title, content, attachments, decoded_attachments)
        if platform == "telegram" and auth_type == "bot":
            return _send_telegram_bot_sample(channel, auth_config, target_config, title, content, attachments)
        if platform == "discord" and auth_type == "bot":
            return _send_discord_bot_message(channel, auth_config, target_config, title, content, attachments, decoded_attachments)
        return "failed", f"Message send is not implemented for {platform} {auth_type} yet", []
    except ProviderConnectivityError as exc:
        return "failed", f"Provider connectivity failed: {exc}", []
    except error.URLError as exc:
        return "failed", f"Provider connectivity failed: {_provider_connectivity_message(exc)}", []
    except Exception as exc:
        return "failed", f"Message send failed: {exc}", []


def api_key_to_schema(api_key: models.ApiKey) -> schemas.ApiKeyResponse:
    return schemas.ApiKeyResponse(
        id=api_key.id,
        name=api_key.name,
        provider=api_key.provider,
        description=api_key.description,
        is_active=api_key.is_active,
        masked_value=_mask_secret(api_key.secret_value),
        created_at=api_key.created_at,
        updated_at=api_key.updated_at,
    )


def webhook_to_schema(webhook: models.Webhook) -> schemas.WebhookResponse:
    return schemas.WebhookResponse(
        id=webhook.id,
        name=webhook.name,
        target_url=webhook.target_url,
        event_scope=webhook.event_scope,
        http_method=webhook.http_method,
        secret=webhook.secret,
        is_enabled=webhook.is_enabled,
        channel_id=webhook.channel_id,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )
