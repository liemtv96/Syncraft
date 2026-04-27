from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import uuid4

from syncraft.errors import HTTPException, status

from syncraft import models, schemas
from syncraft.services.storage import (
    SUPPORTED_PLATFORMS,
    _asset_identity,
    _build_telegram_message_payload,
    _channel_message_identity,
    _channel_message_ordering,
    _load_auth_config,
    _load_target_config,
    _model_to_dict,
    _model_to_json,
    _require_string,
    _sync_discord_channel_assets,
    _sync_discord_channel_messages,
    _sync_mattermost_channel_messages,
    _sync_rocketchat_channel_messages,
    _sync_slack_channel_assets,
    _sync_slack_channel_messages,
    _sync_telegram_bot_messages,
    delete_remote_asset,
    fetch_asset_content,
    send_channel_message,
)


class MongoStorageService:
    def __init__(self, database):
        self.database = database

    def get_settings(self) -> models.AppSettings:
        document = self._collection(models.AppSettings).find_one({"id": 1})
        if document is None:
            now = datetime.utcnow()
            return models.AppSettings(id=1, channel_test_message="Syncraft says hi", created_at=now, updated_at=now)
        settings = self._to_model(models.AppSettings, document)
        if settings.channel_test_message != "Syncraft says hi":
            settings.channel_test_message = "Syncraft says hi"
        return settings

    def list_activity_logs(self, limit: int = 200) -> list[models.ActivityLog]:
        return [
            self._to_model(models.ActivityLog, item)
            for item in self._collection(models.ActivityLog).find().sort("created_at", -1).limit(limit)
        ]

    def clear_activity_logs(self) -> None:
        self._collection(models.ActivityLog).delete_many({})

    def log_activity(self, *, category: str, action: str, title: str, detail: str = "", status: str = "info", entity_type: str = "", entity_id: str = "", channel_id: str | None = None, metadata: dict[str, object] | None = None, commit: bool = True) -> models.ActivityLog:
        now = datetime.utcnow()
        activity = models.ActivityLog(
            id=str(uuid4()),
            category=category,
            action=action,
            status=status,
            title=title,
            detail=detail,
            entity_type=entity_type,
            entity_id=entity_id,
            channel_id=channel_id,
            metadata_json=json.dumps(metadata or {}),
            created_at=now,
        )
        self._replace_model(models.ActivityLog, activity)
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
        self._replace_model(models.AppSettings, settings, default_id=1)
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
        channels = [self._to_model(models.Channel, item) for item in self._collection(models.Channel).find().sort("created_at", -1)]
        return [channel for channel in channels if channel.platform in SUPPORTED_PLATFORMS]

    def list_templates(self) -> list[models.Template]:
        return [self._to_model(models.Template, item) for item in self._collection(models.Template).find().sort("created_at", -1)]

    def create_template(self, payload: schemas.TemplateCreate) -> models.Template:
        template = models.Template(id=str(uuid4()), **_model_to_dict(payload))
        self._replace_model(models.Template, template)
        self.log_activity(category="templates", action="template_created", status="success", title=f"Template created: {template.name}", detail="Saved reusable template", entity_type="template", entity_id=template.id, metadata={"icon": template.icon, "language": template.language or ""})
        return template

    def get_template(self, template_id: str) -> models.Template:
        return self._get_required(models.Template, template_id, "Template not found")

    def update_template(self, template_id: str, payload: schemas.TemplateUpdate) -> models.Template:
        template = self.get_template(template_id)
        for key, value in _model_to_dict(payload, exclude_unset=True).items():
            setattr(template, key, value)
        self._replace_model(models.Template, template)
        self.log_activity(category="templates", action="template_updated", status="success", title=f"Template updated: {template.name}", detail="Updated reusable template", entity_type="template", entity_id=template.id, metadata={"icon": template.icon, "language": template.language or ""})
        return template

    def delete_template(self, template_id: str) -> None:
        template = self.get_template(template_id)
        self._collection(models.Template).delete_one({"id": template_id})
        self.log_activity(category="templates", action="template_deleted", status="warning", title=f"Template removed: {template.name}", detail="Deleted reusable template", entity_type="template", entity_id=template_id)

    def list_variables(self) -> list[models.Variable]:
        return [self._to_model(models.Variable, item) for item in self._collection(models.Variable).find().sort("created_at", -1)]

    def create_variable(self, payload: schemas.VariableCreate) -> models.Variable:
        if self._collection(models.Variable).find_one({"name": payload.name}):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Variable '{payload.name}' already exists")
        variable = models.Variable(id=str(uuid4()), **_model_to_dict(payload))
        self._replace_model(models.Variable, variable)
        self.log_activity(category="variables", action="variable_created", status="success", title=f"Variable created: {variable.name}", detail="Saved reusable variable", entity_type="variable", entity_id=variable.id)
        return variable

    def get_variable(self, variable_id: str) -> models.Variable:
        return self._get_required(models.Variable, variable_id, "Variable not found")

    def update_variable(self, variable_id: str, payload: schemas.VariableUpdate) -> models.Variable:
        variable = self.get_variable(variable_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        next_name = updates.get("name")
        if next_name is not None and next_name != variable.name:
            existing = self._collection(models.Variable).find_one({"name": next_name, "id": {"$ne": variable_id}})
            if existing is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Variable '{next_name}' already exists")
        for key, value in updates.items():
            setattr(variable, key, value)
        self._replace_model(models.Variable, variable)
        self.log_activity(category="variables", action="variable_updated", status="success", title=f"Variable updated: {variable.name}", detail="Updated reusable variable", entity_type="variable", entity_id=variable.id)
        return variable

    def delete_variable(self, variable_id: str) -> None:
        variable = self.get_variable(variable_id)
        self._collection(models.Variable).delete_one({"id": variable_id})
        self.log_activity(category="variables", action="variable_deleted", status="warning", title=f"Variable removed: {variable.name}", detail="Deleted reusable variable", entity_type="variable", entity_id=variable.id)

    def create_channel(self, payload: schemas.ChannelCreate) -> models.Channel:
        duplicate = self._find_duplicate_channel(payload)
        if duplicate is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A channel with the same platform, auth setup, and target already exists")
        now = datetime.utcnow()
        channel = models.Channel(
            id=str(uuid4()),
            name=payload.name,
            platform=payload.platform,
            status=payload.status,
            auth_type=payload.auth_type,
            credentials="",
            capabilities_json=_model_to_json(payload.capabilities),
            channel_identifier="",
            auth_config_json=_model_to_json(payload.auth_config),
            target_config_json=_model_to_json(payload.target_config),
            provider_account_label=payload.provider_account_label,
            target_label=payload.target_label,
            setup_error=payload.setup_error,
            last_test_status=payload.last_test_status,
            last_checked=payload.last_checked,
            asset_sync_cursor="",
            message_sync_cursor="",
            created_at=now,
            updated_at=now,
        )
        self._replace_model(models.Channel, channel)
        self.log_activity(category="channels", action="channel_created", status="success", title=f"Channel created: {channel.name}", detail=f"Configured {channel.platform} channel", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform})
        return channel

    def get_channel(self, channel_id: str) -> models.Channel:
        return self._get_required(models.Channel, channel_id, "Channel not found")

    def update_channel(self, channel_id: str, payload: schemas.ChannelUpdate, log_activity: bool = True) -> models.Channel:
        channel = self.get_channel(channel_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        if "auth_config" in updates:
            next_auth_config = updates.pop("auth_config")
            channel.auth_config_json = _model_to_json(next_auth_config)
            channel.credentials = ""
        if "target_config" in updates:
            next_target_config = updates.pop("target_config")
            channel.target_config_json = _model_to_json(next_target_config)
            channel.channel_identifier = ""
        if "capabilities" in updates:
            channel.capabilities_json = _model_to_json(updates.pop("capabilities"))
        for key, value in updates.items():
            setattr(channel, key, value)
        self._replace_model(models.Channel, channel)
        if log_activity:
            self.log_activity(category="channels", action="channel_updated", status="success", title=f"Channel updated: {channel.name}", detail=f"Updated {channel.platform} channel", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform})
        return channel

    def _find_duplicate_channel(self, payload: schemas.ChannelCreate | schemas.ChannelUpdate, exclude_id: str | None = None) -> models.Channel | None:
        candidates = [self._to_model(models.Channel, item) for item in self._collection(models.Channel).find({"platform": payload.platform, "auth_type": payload.auth_type})]
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
        assets = [self._to_model(models.Asset, item) for item in self._collection(models.Asset).find({"mirror_group_id": mirror_group_id})]
        mirror_count = len(assets)
        for asset in assets:
            asset.mirrors = mirror_count
            self._replace_model(models.Asset, asset)

    def delete_channel(self, channel_id: str) -> None:
        channel = self.get_channel(channel_id)
        self._collection(models.Webhook).delete_many({"channel_id": channel_id})
        for model in (models.Asset, models.ChannelMessage, models.ActivityLog):
            self._collection(model).update_many({"channel_id": channel_id}, {"$set": {"channel_id": None, "updated_at": datetime.utcnow()}})
        self._collection(models.Channel).delete_one({"id": channel_id})
        self.log_activity(category="channels", action="channel_deleted", status="warning", title=f"Channel removed: {channel.name}", detail=f"Deleted {channel.platform} channel {channel.name}", entity_type="channel", entity_id=channel_id, metadata={"platform": channel.platform})

    def list_assets(self) -> list[models.Asset]:
        assets = [self._to_model(models.Asset, item) for item in self._collection(models.Asset).find()]
        deduped: dict[tuple[str, ...], models.Asset] = {}
        for asset in assets:
            key = _asset_identity(asset)
            existing = deduped.get(key)
            if existing is None or asset.updated_at > existing.updated_at:
                deduped[key] = asset
        return sorted(deduped.values(), key=lambda item: item.created_at, reverse=True)

    def list_messages(self) -> list[models.ChannelMessage]:
        messages = [self._to_model(models.ChannelMessage, item) for item in self._collection(models.ChannelMessage).find()]
        deduped: dict[tuple[str, ...], models.ChannelMessage] = {}
        for message in messages:
            key = _channel_message_identity(message)
            existing = deduped.get(key)
            if existing is None or message.updated_at > existing.updated_at:
                deduped[key] = message
        return sorted(deduped.values(), key=lambda item: (item.sent_at is None, -(item.sent_at.timestamp() if item.sent_at else 0), -(item.created_at.timestamp() if item.created_at else 0)))

    def create_asset(self, payload: schemas.AssetCreate) -> models.Asset:
        asset = models.Asset(id=str(uuid4()), channel_id=payload.channel_id, platform=payload.platform, name=payload.name, asset_type=payload.type, mime_type=payload.mime_type, size_bytes=payload.size_bytes, provider_asset_id=payload.provider_asset_id, provider_message_id=payload.provider_message_id, remote_url=payload.remote_url, preview_url=payload.preview_url, mirror_group_id=payload.mirror_group_id, status=payload.status, tags_json=json.dumps(payload.tags), mirrors=payload.mirrors)
        self._replace_model(models.Asset, asset)
        if asset.mirror_group_id:
            self._refresh_mirror_group(asset.mirror_group_id)
        self.log_activity(category="assets", action="asset_created", status="success", title=f"Asset indexed: {asset.name}", detail=f"Added remote {asset.asset_type} asset from {asset.platform}", entity_type="asset", entity_id=asset.id, channel_id=asset.channel_id, metadata={"platform": asset.platform, "size_bytes": asset.size_bytes})
        return asset

    def create_remote_assets(self, payloads: list[schemas.AssetCreate]) -> list[models.Asset]:
        created: list[models.Asset] = []
        mirror_group_ids: set[str] = set()
        for payload in payloads:
            asset = self._upsert_remote_asset(payload)
            created.append(asset)
            if payload.mirror_group_id:
                mirror_group_ids.add(payload.mirror_group_id)
        for mirror_group_id in mirror_group_ids:
            self._refresh_mirror_group(mirror_group_id)
        return created

    def get_asset(self, asset_id: str) -> models.Asset:
        asset = self._get_required(models.Asset, asset_id, "Asset not found")
        if asset.channel is None and asset.channel_id:
            try:
                asset.channel = self.get_channel(asset.channel_id)
            except HTTPException:
                asset.channel = None
        return asset

    def update_asset(self, asset_id: str, payload: schemas.AssetUpdate) -> models.Asset:
        asset = self.get_asset(asset_id)
        updates = _model_to_dict(payload, exclude_unset=True)
        update_keys = sorted(updates.keys())
        if "tags" in updates:
            asset.tags_json = json.dumps(updates.pop("tags"))
        for key, value in updates.items():
            setattr(asset, key, value)
        self._replace_model(models.Asset, asset)
        if update_keys:
            self.log_activity(category="assets", action="asset_updated", status="success", title=f"Asset updated: {asset.name}", detail=f"Updated {', '.join(update_keys)} for asset {asset.name}", entity_type="asset", entity_id=asset.id, channel_id=asset.channel_id, metadata={"updated_fields": update_keys, "platform": asset.platform})
        return asset

    def mirror_asset(self, asset_id: str, channel_ids: list[str]) -> schemas.AssetMirrorResponse:
        asset = self.get_asset(asset_id)
        normalized_channel_ids = list(dict.fromkeys(channel_id for channel_id in channel_ids if channel_id.strip()))
        if not normalized_channel_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Select at least one channel to mirror this asset to")
        mirror_group_id = asset.mirror_group_id or uuid4().hex
        if not asset.mirror_group_id:
            asset.mirror_group_id = mirror_group_id
            self._replace_model(models.Asset, asset)
            self._refresh_mirror_group(mirror_group_id)
        content, mime_type = fetch_asset_content(asset)
        attachment = schemas.BroadcastAttachment(name=asset.name, type=asset.asset_type, size=asset.size_bytes, mime_type=mime_type or asset.mime_type, content_base64=base64.b64encode(content).decode("utf-8"), mirror_group_id=mirror_group_id)
        existing_channel_ids = {item.channel_id for item in self.list_assets() if item.mirror_group_id == mirror_group_id and item.channel_id}
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
            result, detail, created_assets = send_channel_message(channel, attachments=[attachment])
            next_status = "active" if result == "success" else "error"
            updated_channel = self.update_channel(channel_id, schemas.ChannelUpdate(status=next_status, last_checked=datetime.utcnow(), last_test_status=result, setup_error="" if result == "success" else detail))
            if result == "success":
                any_success = True
                mirrored_count += 1
                remote_assets.extend(created_assets)
                existing_channel_ids.add(channel_id)
            results.append(schemas.AssetMirrorChannelResult(channel_id=updated_channel.id, channel_name=updated_channel.name, status=result, message=detail))
        created_records = self.create_remote_assets(remote_assets) if remote_assets else []
        if mirror_group_id and (created_records or mirrored_count > 0):
            self._refresh_mirror_group(mirror_group_id)
        overall_status = "success" if mirrored_count and all(item.status == "success" for item in results if item.status != "skipped") else ("partial" if any_success else "failed")
        self.log_activity(category="assets", action="asset_mirrored" if mirrored_count else "asset_mirror_attempted", status="success" if overall_status == "success" else ("warning" if overall_status == "partial" else "error"), title=f"Asset mirrored: {asset.name}", detail=f"Mirrored asset to {mirrored_count} channel(s); skipped {skipped_count} existing mirror(s)", entity_type="asset", entity_id=asset.id, channel_id=asset.channel_id, metadata={"mirror_group_id": mirror_group_id, "mirrored_count": mirrored_count, "skipped_count": skipped_count})
        return schemas.AssetMirrorResponse(status=overall_status, mirrored_count=mirrored_count, skipped_count=skipped_count, results=results)

    def delete_asset(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        deleted_remote = True
        try:
            delete_remote_asset(asset)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                deleted_remote = False
            else:
                raise
        self._collection(models.Asset).delete_one({"id": asset_id})
        if asset.mirror_group_id:
            self._refresh_mirror_group(asset.mirror_group_id)
        self.log_activity(category="assets", action="asset_deleted" if deleted_remote else "asset_deleted_missing_remote", status="warning", title=f"Asset deleted: {asset.name}", detail=(f"Deleted asset {asset.name} from Assets and the {asset.platform} provider" if deleted_remote else f"Deleted asset {asset.name} from Assets after confirming it was already missing on {asset.platform}"), entity_type="asset", entity_id=asset_id, channel_id=asset.channel_id, metadata={"platform": asset.platform, "deleted_remote": deleted_remote})

    def sync_remote_messages(self) -> dict[str, int]:
        synced_messages = 0
        synced_channels = 0
        skipped_channels = 0
        active_channels = [channel for channel in self.list_channels() if channel.status == "active"]
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
                skipped_channels += len(channels)
                for channel in channels:
                    self.log_activity(category="messages", action="message_sync_skipped", status="warning", title=f"Message sync skipped: {channel.name}", detail=f"Skipped message sync for {channel.platform} channel due to provider error: {exc}", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform, "error": str(exc)})
                continue
            synced_channels += len(channels)
            synced_messages += created
        for channel in non_telegram_channels:
            try:
                created = self.sync_channel_messages(channel)
            except Exception as exc:
                skipped_channels += 1
                self.log_activity(category="messages", action="message_sync_skipped", status="warning", title=f"Message sync skipped: {channel.name}", detail=f"Skipped message sync for {channel.platform} channel due to provider error: {exc}", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform, "error": str(exc)})
                continue
            if created is None:
                skipped_channels += 1
                continue
            synced_channels += 1
            synced_messages += created
        return {"synced_channels": synced_channels, "synced_messages": synced_messages, "skipped_channels": skipped_channels}

    def sync_remote_assets(self) -> dict[str, int]:
        synced_assets = 0
        synced_channels = 0
        skipped_channels = 0
        for channel in [channel for channel in self.list_channels() if channel.status == "active"]:
            try:
                created = self.sync_channel_assets(channel)
            except Exception as exc:
                skipped_channels += 1
                self.log_activity(category="assets", action="asset_sync_skipped", status="warning", title=f"Asset sync skipped: {channel.name}", detail=f"Skipped asset sync for {channel.platform} channel due to provider error: {exc}", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform, "error": str(exc)})
                continue
            if created is None:
                skipped_channels += 1
                continue
            synced_channels += 1
            synced_assets += created
        return {"synced_channels": synced_channels, "synced_assets": synced_assets, "skipped_channels": skipped_channels}

    def sync_channel_assets(self, channel: models.Channel) -> int | None:
        if channel.platform == "slack" and channel.auth_type == "bot":
            created_assets = _sync_slack_channel_assets(channel)
        elif channel.platform == "discord" and channel.auth_type == "bot":
            created_assets = _sync_discord_channel_assets(channel)
        else:
            return None
        for payload in created_assets:
            self._upsert_remote_asset(payload)
        removed_assets = self._remove_missing_channel_assets(channel, created_assets)
        channel.asset_last_synced_at = datetime.utcnow()
        self._replace_model(models.Channel, channel)
        if removed_assets:
            self.log_activity(category="assets", action="asset_sync_reconciled", status="warning", title=f"Asset sync reconciled: {channel.name}", detail=f"Removed {removed_assets} asset(s) that no longer exist on {channel.platform}", entity_type="channel", entity_id=channel.id, channel_id=channel.id, metadata={"platform": channel.platform, "removed_assets": removed_assets})
        return len(created_assets)

    def sync_channel_messages(self, channel: models.Channel) -> int | None:
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
        self._replace_model(models.Channel, channel)
        return len(created_messages)

    def sync_telegram_bot_messages(self, channels: list[models.Channel]) -> int:
        if not channels:
            return 0
        token = _load_auth_config(channels[0]).get_str("bot_token")
        if not token:
            return 0
        target_channels = {_require_string(_load_target_config(channel).values.get("chat_id"), "chat_id"): channel for channel in channels}
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
            self._replace_model(models.Channel, channel)
        return created_count

    def _upsert_remote_asset(self, payload: schemas.AssetCreate) -> models.Asset:
        assets = [self._to_model(models.Asset, item) for item in self._collection(models.Asset).find({"platform": payload.platform, "provider_asset_id": payload.provider_asset_id})]
        assets = [item for item in assets if item.channel_id == payload.channel_id or item.channel_id is None]
        assets.sort(key=lambda item: item.created_at)
        asset = assets[0] if assets else None
        if asset is None:
            asset = models.Asset(id=str(uuid4()), channel_id=payload.channel_id, platform=payload.platform, name=payload.name, asset_type=payload.type, mime_type=payload.mime_type, size_bytes=payload.size_bytes, provider_asset_id=payload.provider_asset_id, provider_message_id=payload.provider_message_id, remote_url=payload.remote_url, preview_url=payload.preview_url, mirror_group_id=payload.mirror_group_id, status=payload.status, tags_json=json.dumps(payload.tags), mirrors=payload.mirrors)
            self._replace_model(models.Asset, asset)
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
        self._replace_model(models.Asset, asset)
        for duplicate in assets[1:]:
            self._collection(models.Asset).delete_one({"id": duplicate.id})
        return asset

    def _upsert_channel_message(self, payload: schemas.ChannelMessageCreate) -> models.ChannelMessage:
        messages = [self._to_model(models.ChannelMessage, item) for item in self._collection(models.ChannelMessage).find({"platform": payload.platform, "provider_message_id": payload.provider_message_id})]
        messages = [item for item in messages if item.provider_chat_id == payload.provider_chat_id or item.provider_chat_id == ""]
        messages.sort(key=lambda item: item.created_at)
        message = messages[0] if messages else None
        if message is None:
            message = models.ChannelMessage(id=str(uuid4()), channel_id=payload.channel_id, platform=payload.platform, provider_chat_id=payload.provider_chat_id, provider_message_id=payload.provider_message_id, author_name=payload.author_name, content=payload.content, attachment_count=payload.attachment_count, external_url=payload.external_url, sent_at=payload.sent_at)
            self._replace_model(models.ChannelMessage, message)
            return message
        message.channel_id = payload.channel_id
        message.provider_chat_id = payload.provider_chat_id
        message.author_name = payload.author_name
        message.content = payload.content
        message.attachment_count = payload.attachment_count
        message.external_url = payload.external_url
        message.sent_at = payload.sent_at
        self._replace_model(models.ChannelMessage, message)
        for duplicate in messages[1:]:
            self._collection(models.ChannelMessage).delete_one({"id": duplicate.id})
        return message

    def _remove_missing_channel_assets(self, channel: models.Channel, remote_assets: list[schemas.AssetCreate]) -> int:
        remote_identities = {_asset_payload_identity(asset) for asset in remote_assets}
        local_assets = [
            self._to_model(models.Asset, item)
            for item in self._collection(models.Asset).find({"channel_id": channel.id, "platform": channel.platform})
        ]
        removed = 0
        for asset in local_assets:
            if _asset_identity(asset) in remote_identities:
                continue
            self._collection(models.Asset).delete_one({"id": asset.id})
            removed += 1
        return removed

    def list_api_keys(self) -> list[models.ApiKey]:
        return [self._to_model(models.ApiKey, item) for item in self._collection(models.ApiKey).find().sort("created_at", -1)]

    def create_api_key(self, payload: schemas.ApiKeyCreate) -> models.ApiKey:
        api_key = models.ApiKey(id=str(uuid4()), **_model_to_dict(payload))
        self._replace_model(models.ApiKey, api_key)
        return api_key

    def update_api_key(self, api_key_id: str, payload: schemas.ApiKeyUpdate) -> models.ApiKey:
        api_key = self._get_required(models.ApiKey, api_key_id, "API key not found")
        for key, value in _model_to_dict(payload, exclude_unset=True).items():
            setattr(api_key, key, value)
        self._replace_model(models.ApiKey, api_key)
        return api_key

    def delete_api_key(self, api_key_id: str) -> None:
        self._get_required(models.ApiKey, api_key_id, "API key not found")
        self._collection(models.ApiKey).delete_one({"id": api_key_id})

    def list_webhooks(self) -> list[models.Webhook]:
        return [self._to_model(models.Webhook, item) for item in self._collection(models.Webhook).find().sort("created_at", -1)]

    def create_webhook(self, payload: schemas.WebhookCreate) -> models.Webhook:
        if payload.channel_id is not None:
            self.get_channel(payload.channel_id)
        webhook = models.Webhook(id=str(uuid4()), **_model_to_dict(payload))
        self._replace_model(models.Webhook, webhook)
        return webhook

    def update_webhook(self, webhook_id: str, payload: schemas.WebhookUpdate) -> models.Webhook:
        webhook = self._get_required(models.Webhook, webhook_id, "Webhook not found")
        updates = _model_to_dict(payload, exclude_unset=True)
        if "channel_id" in updates and updates["channel_id"] is not None:
            self.get_channel(updates["channel_id"])
        for key, value in updates.items():
            setattr(webhook, key, value)
        self._replace_model(models.Webhook, webhook)
        return webhook

    def delete_webhook(self, webhook_id: str) -> None:
        self._get_required(models.Webhook, webhook_id, "Webhook not found")
        self._collection(models.Webhook).delete_one({"id": webhook_id})

    def _collection(self, model):
        return self.database[model.__tablename__]

    def _document(self, model, instance: object, *, default_id: int | str | None = None) -> dict:
        document = {}
        now = datetime.utcnow()
        for column in model.__table__.columns:
            value = getattr(instance, column.name, None)
            if column.name == "id" and not value:
                value = default_id if default_id is not None else str(uuid4())
                setattr(instance, column.name, value)
            if column.name == "created_at" and value is None:
                value = now
                setattr(instance, column.name, value)
            if column.name == "updated_at":
                if getattr(instance, "created_at", None) is None:
                    setattr(instance, "created_at", now)
                value = now
                setattr(instance, column.name, value)
            document[column.name] = value
        document["_id"] = document["id"]
        return document

    def _replace_model(self, model, instance: object, *, default_id: int | str | None = None) -> object:
        document = self._document(model, instance, default_id=default_id)
        self._collection(model).replace_one({"_id": document["_id"]}, document, upsert=True)
        return instance

    def _to_model(self, model, document: dict):
        payload = {key: value for key, value in document.items() if key != "_id"}
        return model(**payload)

    def _get_required(self, model, item_id: str, detail: str):
        document = self._collection(model).find_one({"id": item_id})
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        return self._to_model(model, document)
