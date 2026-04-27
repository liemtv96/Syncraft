from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from syncraft.app import SyncraftApp, dumps_pretty
from syncraft.errors import HTTPException

CLI_DESCRIPTION = (
    " ______     __  __     __   __     ______     ______     ______     ______   ______\n"
    "/\\  ___\\   /\\ \\_\\ \\   /\\ \"-.\\ \\   /\\  ___\\   /\\  == \\   /\\  __ \\   /\\  ___\\ /\\__  _\\\n"
    "\\ \\___  \\  \\ \\____ \\  \\ \\ \\-.  \\  \\ \\ \\____  \\ \\  __<   \\ \\  __ \\  \\ \\  __\\ \\/_/\\ \\/\n"
    " \\/\\_____\\  \\/\\_____\\  \\ \\_\\\\\"\\_\\  \\ \\_____\\  \\ \\_\\ \\_\\  \\ \\_\\ \\_\\  \\ \\_\\      \\ \\_\\\n"
    "  \\/_____/   \\/_____/   \\/_/ \\/_/   \\/_____/   \\/_/ /_/   \\/_/\\/_/   \\/_/       \\/_/\n"
    "\n"
    "Messaging workflow toolkit for local automation and Python integrations\n"
    "\n"
    "Use Syncraft to manage templates, variables, channels, and delivery checks from a local CLI with JSON-friendly output"
)

CLI_EPILOG = (
    "Common commands:\n"
    "  syncraft health\n"
    "  syncraft channel-support\n"
    "  syncraft settings get\n"
    "  syncraft templates list\n"
    "  syncraft templates create --payload '{\"name\":\"Incident\",\"content\":\"Check service health\"}'\n"
    "  syncraft channels list\n"
    "  syncraft channel-sample <channel-id>\n"
    "  syncraft help templates\n"
    "\n"
    "Notes:\n"
    "  - Command results are printed as JSON\n"
    "  - Use `syncraft help <command>` for command-specific usage\n"
    "  - Supported environment variables:\n"
    "      SYNCRAFT_DATABASE_URL\n"
    "      SYNCRAFT_APP_NAME\n"
    "      SYNCRAFT_ENV\n"
    "      SYNCRAFT_DATABASE_ENGINE\n"
    "      SYNCRAFT_PROJECT_ROOT\n"
    "  - Platform data directory variables:\n"
    "      APPDATA\n"
    "      XDG_DATA_HOME\n"
    "  - Set `SYNCRAFT_*` environment variables to control runtime configuration\n"
)

SETTINGS_UPDATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft settings update --payload '{\n"
    "    \"storage_engine\": \"local\",\n"
    "    \"local_db_path\": \"/tmp/syncraft.db\"\n"
    "  }'\n"
)

TEMPLATE_CREATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft templates create --payload '{\n"
    "    \"name\": \"Incident\",\n"
    "    \"description\": \"Reusable alert template\",\n"
    "    \"content\": \"Check service health\",\n"
    "    \"icon\": \"code\"\n"
    "  }'\n"
)

TEMPLATE_UPDATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft templates update TEMPLATE_ID --payload '{\n"
    "    \"content\": \"Investigate latency and error rate\"\n"
    "  }'\n"
)

VARIABLE_CREATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft variables create --payload '{\n"
    "    \"name\": \"service_name\",\n"
    "    \"default_value\": \"billing-api\",\n"
    "    \"description\": \"Default service identifier\"\n"
    "  }'\n"
    "  syncraft variables create --payload-file ./variable.json\n"
)

VARIABLE_UPDATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft variables update VARIABLE_ID --payload '{\n"
    "    \"default_value\": \"auth-api\"\n"
    "  }'\n"
)

CHANNEL_LIST_EXAMPLE = (
    "Examples:\n"
    "  syncraft channels list\n"
    "  syncraft channel-support\n"
)

CHANNEL_CREATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft channels create --payload '{\n"
    "    \"name\": \"Slack Alerts\",\n"
    "    \"platform\": \"slack\",\n"
    "    \"auth_type\": \"webhook\",\n"
    "    \"auth_config\": {\"values\": {\"webhook_url\": \"https://hooks.slack.com/services/XXX/YYY/ZZZ\"}},\n"
    "    \"target_config\": {\"values\": {}},\n"
    "    \"provider_account_label\": \"Slack Webhook\",\n"
    "    \"target_label\": \"Incoming Webhook\"\n"
    "  }'\n"
    "\n"
    "  syncraft channels create --payload '{\n"
    "    \"name\": \"Slack Ops\",\n"
    "    \"platform\": \"slack\",\n"
    "    \"auth_type\": \"bot\",\n"
    "    \"auth_config\": {\"values\": {\"bot_token\": \"xoxb-...\"}},\n"
    "    \"target_config\": {\"values\": {\"channel_id\": \"C12345678\"}},\n"
    "    \"provider_account_label\": \"Slack Bot\",\n"
    "    \"target_label\": \"#ops\"\n"
    "  }'\n"
    "\n"
    "  syncraft channels create --payload '{\n"
    "    \"name\": \"Telegram Alerts\",\n"
    "    \"platform\": \"telegram\",\n"
    "    \"auth_type\": \"bot\",\n"
    "    \"auth_config\": {\"values\": {\"bot_token\": \"123456:ABCDEF\"}},\n"
    "    \"target_config\": {\"values\": {\"chat_id\": \"-1001234567890\"}},\n"
    "    \"provider_account_label\": \"Telegram Bot\",\n"
    "    \"target_label\": \"Ops Chat\"\n"
    "  }'\n"
)

CHANNEL_UPDATE_EXAMPLE = (
    "Examples:\n"
    "  syncraft channels update CHANNEL_ID --payload '{\n"
    "    \"name\": \"Slack Ops Primary\",\n"
    "    \"target_label\": \"#ops-primary\"\n"
    "  }'\n"
    "\n"
    "  syncraft channels update CHANNEL_ID --payload '{\n"
    "    \"auth_config\": {\"values\": {\"bot_token\": \"xoxb-new-token\"}},\n"
    "    \"target_config\": {\"values\": {\"channel_id\": \"C99999999\"}},\n"
    "    \"provider_account_label\": \"Slack Bot\",\n"
    "    \"target_label\": \"#ops-primary\"\n"
    "  }'\n"
    "\n"
    "  syncraft channels update CHANNEL_ID --payload '{\n"
    "    \"auth_config\": {\"values\": {\"webhook_url\": \"https://hooks.slack.com/services/NEW/WEBHOOK/URL\"}}\n"
    "  }'\n"
)

CHANNEL_DELETE_EXAMPLE = (
    "Examples:\n"
    "  syncraft channels delete CHANNEL_ID\n"
)

CHANNEL_TEST_CONFIG_EXAMPLE = (
    "Examples:\n"
    "  syncraft channel-test-config --payload '{\n"
    "    \"name\": \"Slack Test\",\n"
    "    \"platform\": \"slack\",\n"
    "    \"auth_type\": \"webhook\",\n"
    "    \"auth_config\": {\"values\": {\"webhook_url\": \"https://hooks.slack.com/services/XXX/YYY/ZZZ\"}},\n"
    "    \"target_config\": {\"values\": {}}\n"
    "  }'\n"
)

CHANNEL_TEST_EXAMPLE = (
    "Examples:\n"
    "  syncraft channel-test CHANNEL_ID\n"
)

CHANNEL_SAMPLE_EXAMPLE = (
    "Examples:\n"
    "  syncraft channel-sample CHANNEL_ID\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncraft",
        description=CLI_DESCRIPTION,
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    help_parser = subparsers.add_parser("help")
    help_parser.add_argument("topics", nargs="*")

    subparsers.add_parser("health")
    subparsers.add_parser("channel-support")

    settings = subparsers.add_parser("settings")
    settings_subparsers = settings.add_subparsers(dest="action", required=True)
    settings_subparsers.add_parser("get")
    settings_update = settings_subparsers.add_parser(
        "update",
        epilog=SETTINGS_UPDATE_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_payload_arguments(settings_update)

    history = subparsers.add_parser("history")
    history_subparsers = history.add_subparsers(dest="action", required=True)
    history_subparsers.add_parser("list")
    history_subparsers.add_parser("clear")

    for resource in ("templates", "variables", "channels"):
        resource_parser = subparsers.add_parser(resource)
        resource_subparsers = resource_parser.add_subparsers(dest="action", required=True)
        list_parser = resource_subparsers.add_parser(
            "list",
            epilog=CHANNEL_LIST_EXAMPLE if resource == "channels" else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        create = resource_subparsers.add_parser(
            "create",
            epilog={
                "templates": TEMPLATE_CREATE_EXAMPLE,
                "variables": VARIABLE_CREATE_EXAMPLE,
                "channels": CHANNEL_CREATE_EXAMPLE,
            }[resource],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        _add_payload_arguments(create)
        update = resource_subparsers.add_parser(
            "update",
            epilog={
                "templates": TEMPLATE_UPDATE_EXAMPLE,
                "variables": VARIABLE_UPDATE_EXAMPLE,
                "channels": CHANNEL_UPDATE_EXAMPLE,
            }[resource],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        update.add_argument("resource_id")
        _add_payload_arguments(update)
        delete = resource_subparsers.add_parser(
            "delete",
            epilog=CHANNEL_DELETE_EXAMPLE if resource == "channels" else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        delete.add_argument("resource_id")

    channel_test_config = subparsers.add_parser(
        "channel-test-config",
        epilog=CHANNEL_TEST_CONFIG_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_payload_arguments(channel_test_config)

    channel_test = subparsers.add_parser(
        "channel-test",
        epilog=CHANNEL_TEST_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    channel_test.add_argument("channel_id")

    channel_sample = subparsers.add_parser(
        "channel-sample",
        epilog=CHANNEL_SAMPLE_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    channel_sample.add_argument("channel_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv:
        parser.print_help()
        return 0
    if effective_argv in (["settings"], ["history"], ["templates"], ["variables"], ["channels"]):
        target_parser = _resolve_help_parser(parser, effective_argv)
        target_parser.print_help()
        return 0
    try:
        args = parser.parse_args(effective_argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0

    if args.command == "help":
        target_parser = _resolve_help_parser(parser, args.topics)
        target_parser.print_help()
        return 0

    app = SyncraftApp()

    try:
        result = _dispatch(app, args)
    except HTTPException as exc:
        print(dumps_pretty({"error": exc.detail, "status_code": exc.status_code}), file=sys.stderr)
        return 1
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(dumps_pretty({"error": str(exc)}), file=sys.stderr)
        return 2

    if result is not None:
        print(dumps_pretty(result))
    return 0


def _dispatch(app: SyncraftApp, args: argparse.Namespace) -> Any:
    if args.command == "health":
        return app.healthcheck()
    if args.command == "channel-support":
        return app.list_channel_support()
    if args.command == "settings":
        if args.action == "get":
            return app.get_settings()
        return app.update_settings(_read_payload(args))
    if args.command == "history":
        if args.action == "list":
            return app.list_history()
        app.clear_history()
        return {"status": "ok"}
    if args.command == "templates":
        return _dispatch_crud(
            args,
            list_fn=app.list_templates,
            create_fn=app.create_template,
            update_fn=app.update_template,
            delete_fn=app.delete_template,
        )
    if args.command == "variables":
        return _dispatch_crud(
            args,
            list_fn=app.list_variables,
            create_fn=app.create_variable,
            update_fn=app.update_variable,
            delete_fn=app.delete_variable,
        )
    if args.command == "channels":
        return _dispatch_crud(
            args,
            list_fn=app.list_channels,
            create_fn=app.create_channel,
            update_fn=app.update_channel,
            delete_fn=app.delete_channel,
        )
    if args.command == "channel-test-config":
        return app.test_channel_config(_read_payload(args))
    if args.command == "channel-test":
        return app.test_channel(args.channel_id)
    if args.command == "channel-sample":
        return app.send_channel_sample_message(args.channel_id)
    raise ValueError(f"Unsupported command '{args.command}'.")


def _resolve_help_parser(
    root_parser: argparse.ArgumentParser,
    topics: list[str],
) -> argparse.ArgumentParser:
    parser = root_parser
    for topic in topics:
        next_parser = _find_subparser(parser, topic)
        if next_parser is None:
            raise ValueError(f"Unknown help topic '{topic}'")
        parser = next_parser
    return parser


def _find_subparser(
    parser: argparse.ArgumentParser,
    name: str,
) -> argparse.ArgumentParser | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices.get(name)
    return None


def _dispatch_crud(args: argparse.Namespace, *, list_fn, create_fn, update_fn, delete_fn) -> Any:
    if args.action == "list":
        return list_fn()
    if args.action == "create":
        return create_fn(_read_payload(args))
    if args.action == "update":
        return update_fn(args.resource_id, _read_payload(args))
    delete_fn(args.resource_id)
    return {"status": "ok"}


def _add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--payload", help="Inline JSON payload.")
    parser.add_argument("--payload-file", help="Path to a JSON payload file.")


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.payload) == bool(args.payload_file):
        raise ValueError("Provide exactly one of --payload or --payload-file.")
    if args.payload:
        return json.loads(args.payload)
    return json.loads(Path(args.payload_file).read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
