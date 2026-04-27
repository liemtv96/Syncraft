from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from syncraft.cli import main


class CliTests(unittest.TestCase):
    def test_health_command_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "SYNCRAFT_DATABASE_URL": f"sqlite:///{Path(temp_dir) / 'cli.db'}",
                "SYNCRAFT_DATABASE_ENGINE": "sqlite",
                "SYNCRAFT_ENV": "test",
            }
            stdout = io.StringIO()
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(stdout):
                    exit_code = main(["health"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["environment"], "test")

    def test_help_flag_returns_zero(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), patch("sys.stderr", stderr):
            exit_code = main(["--help"])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage:", stdout.getvalue())
        self.assertIn("syncraft", stdout.getvalue())
        self.assertIn("Common commands:", stdout.getvalue())
        self.assertIn("Command results are printed as JSON", stdout.getvalue())
        self.assertIn("syncraft channel-support", stdout.getvalue())
        self.assertIn("Supported environment variables:", stdout.getvalue())
        self.assertIn("SYNCRAFT_DATABASE_URL", stdout.getvalue())
        self.assertIn("SYNCRAFT_APP_NAME", stdout.getvalue())
        self.assertIn("SYNCRAFT_ENV", stdout.getvalue())
        self.assertIn("SYNCRAFT_DATABASE_ENGINE", stdout.getvalue())
        self.assertIn("SYNCRAFT_PROJECT_ROOT", stdout.getvalue())
        self.assertIn("APPDATA", stdout.getvalue())
        self.assertIn("XDG_DATA_HOME", stdout.getvalue())

    def test_help_command_supports_topics(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["help", "templates"])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage:", stdout.getvalue())
        self.assertIn("templates", stdout.getvalue())

    def test_group_command_without_action_prints_help(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["channels"])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: syncraft channels", stdout.getvalue())
        self.assertIn("{list,create,update,delete}", stdout.getvalue())

    def test_nested_help_shows_create_examples(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), patch("sys.stderr", stderr):
            exit_code = main(["channels", "create", "--help"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Examples:", stdout.getvalue())
        self.assertIn("webhook_url", stdout.getvalue())
        self.assertIn("bot_token", stdout.getvalue())
        self.assertIn("chat_id", stdout.getvalue())

    def test_channel_support_lists_supported_provider_shapes(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["channel-support"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(any(item["platform"] == "slack" for item in payload))
        slack = next(item for item in payload if item["platform"] == "slack")
        self.assertTrue(any(setup["auth_type"] == "webhook" for setup in slack["supported_auth_types"]))
        self.assertTrue(any(setup["auth_type"] == "bot" for setup in slack["supported_auth_types"]))

    def test_channel_create_uses_default_capabilities_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "SYNCRAFT_DATABASE_URL": f"sqlite:///{Path(temp_dir) / 'cli.db'}",
                "SYNCRAFT_DATABASE_ENGINE": "sqlite",
                "SYNCRAFT_ENV": "test",
            }
            stdout = io.StringIO()
            payload = json.dumps(
                {
                    "name": "Slack Alerts",
                    "platform": "slack",
                    "auth_type": "webhook",
                    "auth_config": {"values": {"webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ"}},
                    "target_config": {"values": {}},
                }
            )
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(stdout):
                    exit_code = main(["channels", "create", "--payload", payload])

        self.assertEqual(exit_code, 0)
        created = json.loads(stdout.getvalue())
        self.assertEqual(created["name"], "Slack Alerts")
        self.assertEqual(created["capabilities"]["text"], True)
        self.assertEqual(created["capabilities"]["files"], False)

    def test_channel_update_can_replace_auth_and_target_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "SYNCRAFT_DATABASE_URL": f"sqlite:///{Path(temp_dir) / 'cli.db'}",
                "SYNCRAFT_DATABASE_ENGINE": "sqlite",
                "SYNCRAFT_ENV": "test",
            }
            create_stdout = io.StringIO()
            create_payload = json.dumps(
                {
                    "name": "Slack Ops",
                    "platform": "slack",
                    "auth_type": "bot",
                    "auth_config": {"values": {"bot_token": "xoxb-old-token"}},
                    "target_config": {"values": {"channel_id": "C12345678"}},
                }
            )
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(create_stdout):
                    create_exit_code = main(["channels", "create", "--payload", create_payload])

                created = json.loads(create_stdout.getvalue())
                update_stdout = io.StringIO()
                update_payload = json.dumps(
                    {
                        "auth_config": {"values": {"bot_token": "xoxb-new-token"}},
                        "target_config": {"values": {"channel_id": "C99999999"}},
                    }
                )
                with redirect_stdout(update_stdout):
                    update_exit_code = main(["channels", "update", created["id"], "--payload", update_payload])

        self.assertEqual(create_exit_code, 0)
        self.assertEqual(update_exit_code, 0)
        updated = json.loads(update_stdout.getvalue())
        self.assertEqual(updated["auth_config"]["values"]["bot_token"], "xoxb-new-token")
        self.assertEqual(updated["target_config"]["values"]["channel_id"], "C99999999")

    def test_no_args_prints_branded_help(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Syncraft", stdout.getvalue())
        self.assertIn("Common commands:", stdout.getvalue())
        self.assertIn("syncraft channel-support", stdout.getvalue())
        self.assertIn("SYNCRAFT_DATABASE_URL", stdout.getvalue())
        self.assertIn("SYNCRAFT_DATABASE_ENGINE", stdout.getvalue())
        self.assertIn("SYNCRAFT_PROJECT_ROOT", stdout.getvalue())
        self.assertIn("APPDATA", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
