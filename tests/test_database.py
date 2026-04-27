from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from syncraft.config import AppConfig
from syncraft.database import (
    Base,
    DatabaseManager,
    build_database_url,
    build_engine,
    build_session_factory,
    sync_engines,
)
from syncraft.models import AppSettings, Channel, ChannelMessage, Template, Webhook


def make_config(database_url: str, database_engine: str = "sqlite") -> AppConfig:
    temp_root = Path(tempfile.gettempdir()) / "syncraft-tests"
    return AppConfig(
        project_root=temp_root,
        app_name="Syncraft",
        environment="test",
        database_engine=database_engine,
        database_url=database_url,
        default_database_path=temp_root / "default.db",
        local_db_pointer_path=temp_root / "local_db_path.txt",
    )


class DatabaseTests(unittest.TestCase):
    def test_build_database_url_for_remote_sql_engines(self) -> None:
        self.assertEqual(
            build_database_url(
                "postgresql",
                host="db.example.com",
                port="5432",
                database="syncraft",
                username="user",
                password="p@ss word",
            ),
            "postgresql+psycopg://user:p%40ss+word@db.example.com:5432/syncraft",
        )
        self.assertEqual(
            build_database_url(
                "mysql",
                host="mysql.internal",
                port="3306",
                database="syncraft",
            ),
            "mysql+pymysql://mysql.internal:3306/syncraft",
        )

    def test_sync_engines_copies_and_updates_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_engine = build_engine(make_config(f"sqlite:///{temp_path / 'source.db'}"))
            target_engine = build_engine(make_config(f"sqlite:///{temp_path / 'target.db'}"))
            Base.metadata.create_all(bind=source_engine)
            Base.metadata.create_all(bind=target_engine)

            source_session = build_session_factory(source_engine)()
            try:
                source_session.add(
                    AppSettings(
                        id=1,
                        storage_engine="postgresql",
                        db_host="db.example.com",
                        db_database="syncraft",
                        local_db_directory=str(temp_path),
                    )
                )
                channel = Channel(
                    id="channel-1",
                    name="Ops",
                    platform="slack",
                    auth_type="bot",
                    credentials="{}",
                    capabilities_json='{"text": true}',
                )
                source_session.add(channel)
                source_session.add(
                    Template(
                        id="template-1",
                        name="Incident",
                        description="Incident template",
                        content="Check service health",
                    )
                )
                source_session.add(
                    Webhook(
                        id="webhook-1",
                        name="Alert relay",
                        target_url="https://example.com/webhook",
                        channel_id=channel.id,
                    )
                )
                source_session.commit()
            finally:
                source_session.close()

            target_session = build_session_factory(target_engine)()
            try:
                target_session.add(
                    Template(
                        id="template-1",
                        name="Old template",
                        description="outdated",
                        content="old",
                    )
                )
                target_session.commit()
            finally:
                target_session.close()

            sync_engines(source_engine, target_engine)

            verified_session = build_session_factory(target_engine)()
            try:
                settings = verified_session.get(AppSettings, 1)
                template = verified_session.get(Template, "template-1")
                webhook = verified_session.get(Webhook, "webhook-1")
                self.assertIsNotNone(settings)
                self.assertEqual(settings.storage_engine, "postgresql")
                self.assertEqual(template.name, "Incident")
                self.assertEqual(webhook.channel_id, "channel-1")
            finally:
                verified_session.close()

            source_engine.dispose()
            target_engine.dispose()

    def test_database_manager_can_sync_and_switch_between_sqlite_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_url = f"sqlite:///{temp_path / 'source.db'}"
            target_url = f"sqlite:///{temp_path / 'target.db'}"
            manager = DatabaseManager(make_config(source_url))
            Base.metadata.create_all(bind=manager.engine)

            session = manager.session_factory()
            try:
                session.add(
                    Template(
                        id="template-2",
                        name="Welcome",
                        description="Greeting",
                        content="Hello",
                    )
                )
                session.commit()
            finally:
                session.close()

            target_config = replace(manager.config, database_url=target_url)
            manager.sync_to_database(target_config)
            manager.switch_config(target_config)

            switched_session = manager.session_factory()
            try:
                template = switched_session.get(Template, "template-2")
                self.assertIsNotNone(template)
                self.assertEqual(template.name, "Welcome")
            finally:
                switched_session.close()
                manager.engine.dispose()

    def test_sync_engines_nulls_orphaned_channel_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_engine = build_engine(make_config(f"sqlite:///{temp_path / 'source.db'}"))
            target_engine = build_engine(make_config(f"sqlite:///{temp_path / 'target.db'}"))
            Base.metadata.create_all(bind=source_engine)
            Base.metadata.create_all(bind=target_engine)

            source_session = build_session_factory(source_engine)()
            try:
                source_session.add(
                    ChannelMessage(
                        id="message-1",
                        channel_id="missing-channel",
                        platform="discord",
                        provider_message_id="provider-1",
                        author_name="Syncraft",
                        content="orphaned message",
                        attachment_count=0,
                    )
                )
                source_session.commit()
            finally:
                source_session.close()

            sync_engines(source_engine, target_engine)

            verified_session = build_session_factory(target_engine)()
            try:
                message = verified_session.get(ChannelMessage, "message-1")
                self.assertIsNotNone(message)
                self.assertIsNone(message.channel_id)
            finally:
                verified_session.close()
                source_engine.dispose()
                target_engine.dispose()


if __name__ == "__main__":
    unittest.main()
