from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syncraft.app import SyncraftApp
from syncraft.config import AppConfig
from syncraft.database import Base, build_engine, build_session_factory
from syncraft import models


def _build_app_config(temp_dir: str) -> AppConfig:
    db_path = Path(temp_dir) / "contract.db"
    return AppConfig(
        project_root=Path(temp_dir),
        app_name="Syncraft",
        environment="test",
        database_engine="sqlite",
        database_url=f"sqlite:///{db_path}",
        default_database_path=db_path,
        local_db_pointer_path=Path(temp_dir) / "local_db_path.txt",
    )


class ResponseContractTests(unittest.TestCase):
    def test_channel_create_returns_blank_last_test_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = SyncraftApp(_build_app_config(temp_dir))
            try:
                created = app.create_channel(
                    {
                        "name": "Slack Alerts",
                        "platform": "slack",
                        "auth_type": "webhook",
                        "auth_config": {"values": {"webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ"}},
                        "target_config": {"values": {}},
                    }
                )
            finally:
                app.close()

        self.assertEqual(created.last_test_status, "")

    def test_channel_list_normalizes_legacy_untested_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _build_app_config(temp_dir)
            engine = build_engine(config)
            Base.metadata.create_all(bind=engine)
            session = build_session_factory(engine)()
            try:
                session.add(
                    models.Channel(
                        id="channel-1",
                        name="Legacy Slack",
                        platform="slack",
                        status="active",
                        auth_type="webhook",
                        credentials="{}",
                        capabilities_json='{"text": true, "files": false, "images": false, "video": false}',
                        auth_config_json='{"webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ"}',
                        target_config_json="{}",
                        last_test_status="untested",
                    )
                )
                session.commit()
            finally:
                session.close()
                engine.dispose()

            app = SyncraftApp(config)
            try:
                channels = app.list_channels()
            finally:
                app.close()

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].last_test_status, "")

    def test_channel_test_and_sample_return_documented_status_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = SyncraftApp(_build_app_config(temp_dir))
            try:
                created = app.create_channel(
                    {
                        "name": "Slack Alerts",
                        "platform": "slack",
                        "auth_type": "webhook",
                        "auth_config": {"values": {"webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ"}},
                        "target_config": {"values": {}},
                    }
                )

                with patch("syncraft.app.test_channel_configuration", return_value=("success", "ok")):
                    tested = app.test_channel(created.id)
                with patch("syncraft.app.send_channel_sample", return_value=("success", "sent")):
                    sampled = app.send_channel_sample_message(created.id)
            finally:
                app.close()

        self.assertEqual(tested.status, "active")
        self.assertEqual(tested.last_test_status, "success")
        self.assertEqual(sampled.status, "active")
        self.assertEqual(sampled.last_test_status, "success")


if __name__ == "__main__":
    unittest.main()
