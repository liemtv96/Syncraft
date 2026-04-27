from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from syncraft.config import AppConfig, load_config
from syncraft.database import (
    Base,
    DatabaseManager,
    build_database_url,
    ensure_runtime_schema,
    sqlite_path_from_url,
    write_local_db_pointer,
)
from syncraft.provider_registry import list_provider_support
from syncraft.schemas import (
    ApiKeyCreate,
    ApiKeyUpdate,
    AssetCreate,
    AssetMirrorRequest,
    AssetUpdate,
    BroadcastAttachment,
    BroadcastChannelResult,
    BroadcastRequest,
    BroadcastResponse,
    ChannelCreate,
    ChannelUpdate,
    SettingsPayload,
    TemplateCreate,
    TemplateUpdate,
    VariableCreate,
    VariableUpdate,
    WebhookCreate,
    WebhookUpdate,
)
from syncraft.services.storage import (
    activity_log_to_schema,
    api_key_to_schema,
    asset_to_schema,
    channel_to_schema,
    fetch_asset_content,
    message_to_schema,
    send_channel_message,
    send_channel_sample,
    settings_to_schema_with_config,
    test_channel_configuration,
    webhook_to_schema,
)
from syncraft.storage_backend import StorageBackend, build_storage_dependency


class SyncraftApp:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        self.database_manager = DatabaseManager(self.config)
        if self.database_manager.engine is not None:
            ensure_runtime_schema(self.database_manager.engine)
            Base.metadata.create_all(bind=self.database_manager.engine)
        self._storage_dependency = build_storage_dependency(self.database_manager)

    @contextmanager
    def service(self) -> StorageBackend:
        generator = self._storage_dependency()
        service = next(generator)
        try:
            yield service
        finally:
            generator.close()

    def healthcheck(self) -> dict[str, str]:
        config = self.database_manager.config
        return {
            "status": "ok",
            "environment": config.environment,
            "database_engine": config.database_engine,
        }

    def get_settings(self):
        with self.service() as service:
            return settings_to_schema_with_config(service.get_settings(), self.database_manager.config)

    def list_history(self):
        with self.service() as service:
            return [activity_log_to_schema(item) for item in service.list_activity_logs()]

    def clear_history(self) -> None:
        with self.service() as service:
            service.clear_activity_logs()

    def update_settings(self, payload: SettingsPayload | dict[str, Any]):
        payload = self._coerce_model(payload, SettingsPayload)
        with self.service() as service:
            if payload.storage_engine == "dynamodb":
                raise ValueError(f"Storage engine '{payload.storage_engine}' is not implemented yet")
            current_config = self.database_manager.config
            target_config = current_config
            if payload.storage_engine == "local":
                current_path = (
                    sqlite_path_from_url(current_config.database_url)
                    if current_config.database_engine == "sqlite"
                    else current_config.default_database_path.expanduser().resolve()
                )
                requested_location = payload.local_db_path.strip() or str(current_path)
                resolved_requested_path = Path(requested_location).expanduser().resolve()
                requested_db_path = (
                    resolved_requested_path
                    if resolved_requested_path.suffix.lower() == ".db"
                    else resolved_requested_path / current_path.name
                )
                target_config = replace(
                    current_config,
                    database_engine="sqlite",
                    database_url=f"sqlite:///{requested_db_path}",
                )
            elif payload.storage_engine in {"postgresql", "mysql", "mongodb"}:
                target_config = replace(
                    current_config,
                    database_engine=payload.storage_engine,
                    database_url=build_database_url(
                        payload.storage_engine,
                        host=payload.db_connection.host,
                        port=payload.db_connection.port,
                        database=payload.db_connection.database,
                        username=payload.db_connection.username,
                        password=payload.db_connection.password,
                    ),
                )

            if (
                target_config.database_engine != current_config.database_engine
                or target_config.database_url != current_config.database_url
            ):
                self.database_manager.sync_to_database(target_config)
                if target_config.database_engine == "sqlite":
                    write_path = sqlite_path_from_url(target_config.database_url)
                    write_local_db_pointer(current_config, write_path)
                self.database_manager.switch_config(target_config)
                with self.service() as active_service:
                    settings = active_service.update_settings(payload)
                    return settings_to_schema_with_config(settings, self.database_manager.config)

            settings = service.update_settings(payload)
            return settings_to_schema_with_config(settings, self.database_manager.config)

    def list_channels(self):
        with self.service() as service:
            return [channel_to_schema(channel) for channel in service.list_channels()]

    def list_channel_support(self):
        return list_provider_support()

    def create_channel(self, payload: ChannelCreate | dict[str, Any]):
        payload = self._coerce_model(payload, ChannelCreate)
        with self.service() as service:
            return channel_to_schema(service.create_channel(payload))

    def test_channel_config(self, payload: ChannelCreate | dict[str, Any]) -> dict[str, str]:
        payload = self._coerce_model(payload, ChannelCreate)
        result, message = test_channel_configuration(payload)
        return {"status": result, "message": message}

    def update_channel(self, channel_id: str, payload: ChannelUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, ChannelUpdate)
        with self.service() as service:
            return channel_to_schema(service.update_channel(channel_id, payload))

    def delete_channel(self, channel_id: str) -> None:
        with self.service() as service:
            service.delete_channel(channel_id)

    def test_channel(self, channel_id: str):
        with self.service() as service:
            channel = service.get_channel(channel_id)
            result, message = test_channel_configuration(channel)
            next_status = "active" if result == "success" else "error"
            updated = service.update_channel(
                channel_id,
                ChannelUpdate(
                    status=next_status,
                    last_checked=datetime.utcnow(),
                    last_test_status=result,
                    setup_error="" if result == "success" else message,
                ),
                log_activity=False,
            )
            service.log_activity(
                category="channels",
                action="channel_tested",
                status="success" if result == "success" else "error",
                title=f"Channel test {'passed' if result == 'success' else 'failed'}: {updated.name}",
                detail=message,
                entity_type="channel",
                entity_id=updated.id,
                channel_id=updated.id,
                metadata={"platform": updated.platform, "result": result},
            )
            return channel_to_schema(updated)

    def send_channel_sample_message(self, channel_id: str):
        with self.service() as service:
            channel = service.get_channel(channel_id)
            settings = service.get_settings()
            result, message = send_channel_sample(channel, settings.channel_test_message)
            next_status = "active" if result == "success" else "error"
            updated = service.update_channel(
                channel_id,
                ChannelUpdate(
                    status=next_status,
                    last_checked=datetime.utcnow(),
                    last_test_status=result,
                    setup_error="" if result == "success" else message,
                ),
                log_activity=False,
            )
            service.log_activity(
                category="channels",
                action="channel_sample_sent",
                status="success" if result == "success" else "error",
                title=f"Sample message {'sent' if result == 'success' else 'failed'}: {updated.name}",
                detail=message,
                entity_type="channel",
                entity_id=updated.id,
                channel_id=updated.id,
                metadata={"platform": updated.platform, "result": result},
            )
            return channel_to_schema(updated)

    def list_templates(self):
        with self.service() as service:
            return list(service.list_templates())

    def create_template(self, payload: TemplateCreate | dict[str, Any]):
        payload = self._coerce_model(payload, TemplateCreate)
        with self.service() as service:
            return service.create_template(payload)

    def update_template(self, template_id: str, payload: TemplateUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, TemplateUpdate)
        with self.service() as service:
            return service.update_template(template_id, payload)

    def delete_template(self, template_id: str) -> None:
        with self.service() as service:
            service.delete_template(template_id)

    def list_variables(self):
        with self.service() as service:
            return list(service.list_variables())

    def create_variable(self, payload: VariableCreate | dict[str, Any]):
        payload = self._coerce_model(payload, VariableCreate)
        with self.service() as service:
            return service.create_variable(payload)

    def update_variable(self, variable_id: str, payload: VariableUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, VariableUpdate)
        with self.service() as service:
            return service.update_variable(variable_id, payload)

    def delete_variable(self, variable_id: str) -> None:
        with self.service() as service:
            service.delete_variable(variable_id)

    def broadcast_content(self, payload: BroadcastRequest | dict[str, Any]) -> BroadcastResponse:
        payload = self._coerce_model(payload, BroadcastRequest)
        if not payload.title.strip() and not payload.content.strip() and not payload.attachments:
            return BroadcastResponse(status="failed", results=[])

        attachments = [
            attachment.model_copy(update={"mirror_group_id": attachment.mirror_group_id or uuid4().hex})
            for attachment in payload.attachments
        ]
        results: list[BroadcastChannelResult] = []
        remote_assets: list[AssetCreate] = []
        with self.service() as service:
            for channel_id in payload.channel_ids:
                channel = service.get_channel(channel_id)
                result, detail, created_assets = send_channel_message(
                    channel,
                    title=payload.title,
                    content=payload.content,
                    attachments=attachments,
                )
                next_status = "active" if result == "success" else "error"
                updated = service.update_channel(
                    channel_id,
                    ChannelUpdate(
                        status=next_status,
                        last_checked=datetime.utcnow(),
                        last_test_status=result,
                        setup_error="" if result == "success" else detail,
                    ),
                )
                if result == "success":
                    remote_assets.extend(created_assets)
                results.append(
                    BroadcastChannelResult(
                        channel_id=updated.id,
                        channel_name=updated.name,
                        status=result,
                        message=detail,
                    )
                )
            if remote_assets:
                created_assets = service.create_remote_assets(remote_assets)
            else:
                created_assets = []
            overall_status = self._broadcast_status(results)
            service.log_activity(
                category="broadcasts",
                action="broadcast_sent" if overall_status == "success" else "broadcast_completed",
                status="success" if overall_status == "success" else ("warning" if overall_status == "partial" else "error"),
                title="Asset upload processed" if payload.asset_source == "assets" else "Push content processed",
                detail=(
                    f"Processed broadcast to {len(results)} channels with {len(payload.attachments)} attachments. "
                    f"Recorded {len(created_assets)} remote assets"
                ),
                entity_type="broadcast",
                entity_id="push-content",
                metadata={
                    "status": overall_status,
                    "channel_count": len(results),
                    "attachment_count": len(payload.attachments),
                    "asset_source": payload.asset_source,
                    "successful_channels": len([item for item in results if item.status == "success"]),
                },
            )
            return BroadcastResponse(status=overall_status, results=results)

    def list_assets(self):
        with self.service() as service:
            return [asset_to_schema(asset) for asset in service.list_assets()]

    def sync_assets(self) -> dict[str, int]:
        with self.service() as service:
            return service.sync_remote_assets()

    def list_messages(self):
        with self.service() as service:
            return [message_to_schema(message) for message in service.list_messages()]

    def sync_messages(self) -> dict[str, int]:
        with self.service() as service:
            return service.sync_remote_messages()

    def create_asset(self, payload: AssetCreate | dict[str, Any]):
        payload = self._coerce_model(payload, AssetCreate)
        with self.service() as service:
            return asset_to_schema(service.create_asset(payload))

    def update_asset(self, asset_id: str, payload: AssetUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, AssetUpdate)
        with self.service() as service:
            return asset_to_schema(service.update_asset(asset_id, payload))

    def mirror_asset(self, asset_id: str, payload: AssetMirrorRequest | dict[str, Any]):
        payload = self._coerce_model(payload, AssetMirrorRequest)
        with self.service() as service:
            return service.mirror_asset(asset_id, payload.channel_ids)

    def delete_asset(self, asset_id: str) -> None:
        with self.service() as service:
            service.delete_asset(asset_id)

    def get_asset_content(self, asset_id: str, download: bool = False) -> dict[str, Any]:
        with self.service() as service:
            asset = service.get_asset(asset_id)
            content, media_type = fetch_asset_content(asset)
            disposition = "attachment" if download else "inline"
            return {
                "content": content,
                "media_type": media_type,
                "headers": {"Content-Disposition": f'{disposition}; filename="{asset.name}"'},
            }

    def list_api_keys(self):
        with self.service() as service:
            return [api_key_to_schema(item) for item in service.list_api_keys()]

    def create_api_key(self, payload: ApiKeyCreate | dict[str, Any]):
        payload = self._coerce_model(payload, ApiKeyCreate)
        with self.service() as service:
            return api_key_to_schema(service.create_api_key(payload))

    def update_api_key(self, api_key_id: str, payload: ApiKeyUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, ApiKeyUpdate)
        with self.service() as service:
            return api_key_to_schema(service.update_api_key(api_key_id, payload))

    def delete_api_key(self, api_key_id: str) -> None:
        with self.service() as service:
            service.delete_api_key(api_key_id)

    def list_webhooks(self):
        with self.service() as service:
            return [webhook_to_schema(item) for item in service.list_webhooks()]

    def create_webhook(self, payload: WebhookCreate | dict[str, Any]):
        payload = self._coerce_model(payload, WebhookCreate)
        with self.service() as service:
            return webhook_to_schema(service.create_webhook(payload))

    def update_webhook(self, webhook_id: str, payload: WebhookUpdate | dict[str, Any]):
        payload = self._coerce_model(payload, WebhookUpdate)
        with self.service() as service:
            return webhook_to_schema(service.update_webhook(webhook_id, payload))

    def delete_webhook(self, webhook_id: str) -> None:
        with self.service() as service:
            service.delete_webhook(webhook_id)

    @staticmethod
    def _coerce_model(payload: BaseModel | dict[str, Any], model_type):
        if isinstance(payload, model_type):
            return payload
        if isinstance(payload, BaseModel):
            return model_type(**payload.model_dump())
        return model_type(**payload)

    @staticmethod
    def _broadcast_status(results: list[BroadcastChannelResult]) -> str:
        if results and all(item.status == "success" for item in results):
            return "success"
        if results and any(item.status == "success" for item in results):
            return "partial"
        return "failed"


def to_plain_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain_data(item) for key, item in value.items()}
    table = getattr(value, "__table__", None)
    if table is not None:
        return {column.name: to_plain_data(getattr(value, column.name)) for column in table.columns}
    return value


def dumps_pretty(value: Any) -> str:
    return json.dumps(to_plain_data(value), indent=2, sort_keys=True)
