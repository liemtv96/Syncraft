from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import SAWarning
from sqlalchemy.dialects import mysql, postgresql, sqlite
from syncraft.errors import HTTPException, status

from syncraft import models
from syncraft.config import AppConfig
from syncraft.database import Base, build_engine, build_session_factory
from syncraft import schemas
from syncraft.services.storage import _asset_payload_identity, _build_telegram_asset_from_message, _channel_message_ordering
from syncraft.services.storage import StorageService


class StorageQueryTests(unittest.TestCase):
    def test_channel_message_ordering_is_mysql_compatible(self) -> None:
        statement = (
            select(models.ChannelMessage.id)
            .order_by(*_channel_message_ordering())
        )
        compiled = str(statement.compile(dialect=mysql.dialect()))
        self.assertNotIn("NULLS LAST", compiled)
        self.assertIn("channel_messages.sent_at IS NULL ASC", compiled)

    def test_channel_message_ordering_compiles_for_supported_sql_dialects(self) -> None:
        statement = (
            select(models.ChannelMessage.id)
            .order_by(*_channel_message_ordering())
        )
        for dialect in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
            compiled = str(statement.compile(dialect=dialect))
            self.assertIn("ORDER BY", compiled)
            self.assertNotIn("NULLS LAST", compiled)

    def test_delete_asset_does_not_remove_local_record_when_remote_delete_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            config = AppConfig(
                project_root=Path(temp_dir),
                app_name="Syncraft",
                environment="test",
                database_engine="sqlite",
                database_url=f"sqlite:///{db_path}",
                default_database_path=db_path,
                local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
            )
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                asset = models.Asset(
                    id="asset-1",
                    channel_id="channel-1",
                    platform="discord",
                    name="asset.png",
                    asset_type="image",
                    mime_type="image/png",
                    size_bytes=123,
                    provider_asset_id="provider-asset-1",
                    provider_message_id="provider-message-1",
                )
                session.add(asset)
                session.commit()

                service = StorageService(session)
                with patch(
                    "syncraft.services.storage.delete_remote_asset",
                    side_effect=HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Discord asset is not linked to a channel.",
                    ),
                ):
                    with self.assertRaises(HTTPException):
                        service.delete_asset("asset-1")

                self.assertIsNotNone(session.get(models.Asset, "asset-1"))
            finally:
                session.close()
                engine.dispose()

    def test_remove_missing_channel_assets_reconciles_deleted_remote_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            config = AppConfig(
                project_root=Path(temp_dir),
                app_name="Syncraft",
                environment="test",
                database_engine="sqlite",
                database_url=f"sqlite:///{db_path}",
                default_database_path=db_path,
                local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
            )
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                channel = models.Channel(
                    id="channel-1",
                    name="Ops",
                    platform="discord",
                    status="active",
                    auth_type="bot",
                    credentials="{}",
                    capabilities_json='{"text": true}',
                )
                kept_asset = models.Asset(
                    id="asset-keep",
                    channel_id="channel-1",
                    platform="discord",
                    name="keep.png",
                    asset_type="image",
                    mime_type="image/png",
                    size_bytes=100,
                    provider_asset_id="provider-keep",
                )
                missing_asset = models.Asset(
                    id="asset-missing",
                    channel_id="channel-1",
                    platform="discord",
                    name="missing.png",
                    asset_type="image",
                    mime_type="image/png",
                    size_bytes=200,
                    provider_asset_id="provider-missing",
                )
                session.add(channel)
                session.add(kept_asset)
                session.add(missing_asset)
                session.commit()

                service = StorageService(session)
                removed = service._remove_missing_channel_assets(
                    channel,
                    [
                        schemas.AssetCreate(
                            channel_id="channel-1",
                            platform="discord",
                            name="keep.png",
                            type="image",
                            mime_type="image/png",
                            size_bytes=100,
                            provider_asset_id="provider-keep",
                        )
                    ],
                )
                session.commit()
                self.assertEqual(removed, 1)
                self.assertIsNotNone(session.get(models.Asset, "asset-keep"))
                self.assertIsNone(session.get(models.Asset, "asset-missing"))
            finally:
                session.close()
                engine.dispose()

    def test_asset_reconciliation_flushes_pending_duplicate_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            config = AppConfig(
                project_root=Path(temp_dir),
                app_name="Syncraft",
                environment="test",
                database_engine="sqlite",
                database_url=f"sqlite:///{db_path}",
                default_database_path=db_path,
                local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
            )
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                channel = models.Channel(
                    id="channel-1",
                    name="Ops",
                    platform="discord",
                    status="active",
                    auth_type="bot",
                    credentials="{}",
                    capabilities_json='{"text": true}',
                )
                session.add(channel)
                session.add(
                    models.Asset(
                        id="asset-1",
                        channel_id="channel-1",
                        platform="discord",
                        name="keep.png",
                        asset_type="image",
                        mime_type="image/png",
                        size_bytes=100,
                        provider_asset_id="provider-keep",
                    )
                )
                session.add(
                    models.Asset(
                        id="asset-2",
                        channel_id="channel-1",
                        platform="discord",
                        name="keep-duplicate.png",
                        asset_type="image",
                        mime_type="image/png",
                        size_bytes=100,
                        provider_asset_id="provider-keep",
                    )
                )
                session.commit()

                service = StorageService(session)
                payload = schemas.AssetCreate(
                    channel_id="channel-1",
                    platform="discord",
                    name="keep.png",
                    type="image",
                    mime_type="image/png",
                    size_bytes=100,
                    provider_asset_id="provider-keep",
                )

                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", SAWarning)
                    service._upsert_remote_asset(payload)
                    removed = service._remove_missing_channel_assets(channel, [payload])
                    session.commit()

                asset_warnings = [
                    warning
                    for warning in caught
                    if "DELETE statement on table 'assets'" in str(warning.message)
                ]
                self.assertEqual(removed, 0)
                self.assertEqual(asset_warnings, [])
                remaining_assets = session.query(models.Asset).filter(models.Asset.channel_id == "channel-1").all()
                self.assertEqual(len(remaining_assets), 1)
                self.assertEqual(remaining_assets[0].provider_asset_id, "provider-keep")
            finally:
                session.close()
                engine.dispose()

    def test_asset_payload_identity_matches_model_identity_contract(self) -> None:
        payload = schemas.AssetCreate(
            channel_id="channel-1",
            platform="discord",
            name="file.png",
            type="image",
            mime_type="image/png",
            size_bytes=128,
            provider_asset_id="provider-1",
        )
        self.assertEqual(_asset_payload_identity(payload), ("discord", "provider-1"))

    def test_telegram_message_asset_payload_uses_syncraft_schema(self) -> None:
        channel = models.Channel(
            id="channel-1",
            name="Telegram Ops",
            platform="telegram",
            status="active",
            auth_type="bot",
            credentials="{}",
            capabilities_json='{"text": true}',
        )
        payload = _build_telegram_asset_from_message(
            channel,
            {
                "message_id": 42,
                "document": {
                    "file_id": "telegram-file-1",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 2048,
                },
            },
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.channel_id, "channel-1")
        self.assertEqual(payload.platform, "telegram")
        self.assertEqual(payload.provider_asset_id, "telegram-file-1")
        self.assertEqual(payload.provider_message_id, "42")
        self.assertEqual(payload.name, "report.pdf")
        self.assertEqual(payload.type, "document")

    def test_imported_provider_asset_sync_does_not_reconcile_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            config = AppConfig(
                project_root=Path(temp_dir),
                app_name="Syncraft",
                environment="test",
                database_engine="sqlite",
                database_url=f"sqlite:///{db_path}",
                default_database_path=db_path,
                local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
            )
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                channel = models.Channel(
                    id="channel-1",
                    name="Mattermost Ops",
                    platform="mattermost",
                    status="active",
                    auth_type="bot",
                    credentials="{}",
                    auth_config_json='{"values": {"access_token": "token", "server_url": "https://mattermost.test"}}',
                    target_config_json='{"values": {"channel_id": "channel-id"}}',
                    capabilities_json='{"text": true}',
                )
                existing_asset = models.Asset(
                    id="asset-existing",
                    channel_id="channel-1",
                    platform="mattermost",
                    name="existing.png",
                    asset_type="image",
                    mime_type="image/png",
                    size_bytes=100,
                    provider_asset_id="existing-provider-asset",
                )
                session.add(channel)
                session.add(existing_asset)
                session.commit()

                service = StorageService(session)
                synced_payload = schemas.AssetCreate(
                    channel_id="channel-1",
                    platform="mattermost",
                    name="synced.png",
                    type="image",
                    mime_type="image/png",
                    size_bytes=128,
                    provider_asset_id="synced-provider-asset",
                )
                with patch("syncraft.services.storage._sync_mattermost_channel_assets", return_value=[synced_payload]):
                    with patch.object(service, "_remove_missing_channel_assets") as remove_missing:
                        self.assertEqual(service.sync_channel_assets(channel), 1)

                remove_missing.assert_not_called()
                self.assertIsNotNone(session.get(models.Asset, "asset-existing"))
                synced_assets = (
                    session.query(models.Asset)
                    .filter(models.Asset.provider_asset_id == "synced-provider-asset")
                    .all()
                )
                self.assertEqual(len(synced_assets), 1)
            finally:
                session.close()
                engine.dispose()

    def test_get_asset_hydrates_channel_relation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            config = AppConfig(
                project_root=Path(temp_dir),
                app_name="Syncraft",
                environment="test",
                database_engine="sqlite",
                database_url=f"sqlite:///{db_path}",
                default_database_path=db_path,
                local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
            )
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                channel = models.Channel(
                    id="channel-1",
                    name="Ops",
                    platform="discord",
                    status="active",
                    auth_type="bot",
                    credentials="{}",
                    capabilities_json='{"text": true}',
                )
                asset = models.Asset(
                    id="asset-1",
                    channel_id="channel-1",
                    platform="discord",
                    name="asset.png",
                    asset_type="image",
                    mime_type="image/png",
                    size_bytes=123,
                    provider_asset_id="provider-asset-1",
                    provider_message_id="provider-message-1",
                )
                session.add(channel)
                session.add(asset)
                session.commit()

                service = StorageService(session)
                hydrated_asset = service.get_asset("asset-1")
                self.assertIsNotNone(hydrated_asset.channel)
                self.assertEqual(hydrated_asset.channel.id, "channel-1")
            finally:
                session.close()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
