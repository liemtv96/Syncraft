from __future__ import annotations

from dataclasses import dataclass

from syncraft.schemas import AuthType, Platform


@dataclass(frozen=True)
class ProviderSetup:
    auth_type: AuthType
    auth_fields: tuple[str, ...]
    target_fields: tuple[str, ...]
    test_message: str


@dataclass(frozen=True)
class ProviderDefinition:
    platform: Platform
    setups: tuple[ProviderSetup, ...]


PROVIDER_DEFINITIONS: dict[Platform, ProviderDefinition] = {
    "telegram": ProviderDefinition(
        platform="telegram",
        setups=(
            ProviderSetup(
                auth_type="bot",
                auth_fields=("bot_token",),
                target_fields=("chat_id",),
                test_message="Validated Telegram bot token and chat target shape",
            ),
        ),
    ),
    "slack": ProviderDefinition(
        platform="slack",
        setups=(
            ProviderSetup(
                auth_type="webhook",
                auth_fields=("webhook_url",),
                target_fields=(),
                test_message="Validated Slack incoming webhook configuration",
            ),
            ProviderSetup(
                auth_type="bot",
                auth_fields=("bot_token",),
                target_fields=("channel_id",),
                test_message="Validated Slack bot token and channel target shape",
            ),
        ),
    ),
    "discord": ProviderDefinition(
        platform="discord",
        setups=(
            ProviderSetup(
                auth_type="bot",
                auth_fields=("bot_token",),
                target_fields=("channel_id",),
                test_message="Validated Discord bot token and channel target shape",
            ),
        ),
    ),
    "mattermost": ProviderDefinition(
        platform="mattermost",
        setups=(
            ProviderSetup(
                auth_type="webhook",
                auth_fields=("webhook_url",),
                target_fields=(),
                test_message="Validated Mattermost incoming webhook configuration",
            ),
            ProviderSetup(
                auth_type="bot",
                auth_fields=("server_url", "access_token"),
                target_fields=("channel_id",),
                test_message="Validated Mattermost bot token and channel target shape",
            ),
        ),
    ),
    "rocketchat": ProviderDefinition(
        platform="rocketchat",
        setups=(
            ProviderSetup(
                auth_type="webhook",
                auth_fields=("webhook_url",),
                target_fields=(),
                test_message="Validated Rocket.Chat incoming webhook configuration",
            ),
            ProviderSetup(
                auth_type="bot",
                auth_fields=("server_url", "auth_token", "user_id"),
                target_fields=("room_id",),
                test_message="Validated Rocket.Chat token and room target shape",
            ),
        ),
    ),
}


def list_provider_support() -> list[dict[str, object]]:
    return [
        {
            "platform": definition.platform,
            "supported_auth_types": [
                {
                    "auth_type": setup.auth_type,
                    "required_auth_fields": list(setup.auth_fields),
                    "required_target_fields": list(setup.target_fields),
                    "test_message": setup.test_message,
                }
                for setup in definition.setups
            ],
        }
        for definition in PROVIDER_DEFINITIONS.values()
    ]


def get_provider_setup(platform: Platform, auth_type: AuthType) -> ProviderSetup:
    definition = PROVIDER_DEFINITIONS[platform]
    for setup in definition.setups:
        if setup.auth_type == auth_type:
            return setup
    raise ValueError(f"Unsupported auth type '{auth_type}' for platform '{platform}'")
