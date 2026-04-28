![Syncraft banner](https://raw.githubusercontent.com/liemtv96/Syncraft/main/assets/syncraft-banner.png)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](#install)
[![CLI](https://img.shields.io/badge/CLI-Developer%20Tool-0A0A0A?logo=gnubash&logoColor=white)](#cli-usage)
[![Pydantic](https://img.shields.io/badge/Pydantic-Data%20Models-E92063?logo=pydantic&logoColor=white)](#python-usage)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-ORM-D71F00?logo=sqlalchemy&logoColor=white)](#python-usage)
[![SQLite](https://img.shields.io/badge/SQLite-Storage-003B57?logo=sqlite&logoColor=white)](#configuration)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Optional-336791?logo=postgresql&logoColor=white)](#install)
[![MongoDB](https://img.shields.io/badge/MongoDB-Optional-47A248?logo=mongodb&logoColor=white)](#install)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license)

Standalone Python package and CLI for synchronized messaging workflows. It keeps the storage, schema, provider, and synchronization logic, but exposes it as:

- a Python package for direct imports
- a CLI for local tooling and automation

## Install

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install syncraft-channel-weave
python -m syncraft health
syncraft health
```

Optional storage extras:

```sh
python -m pip install "syncraft-channel-weave[postgresql]"
python -m pip install "syncraft-channel-weave[mysql]"
python -m pip install "syncraft-channel-weave[mongodb]"
```

## CLI usage

```sh
syncraft health
syncraft channel-support
syncraft settings get
syncraft templates list
syncraft templates create --payload '{"name":"Incident","content":"Check service health"}'
syncraft variables create --payload '{"name":"service_name","default_value":"billing-api"}'
syncraft channels list
```

## Python usage

The verified import surface for channel registration/testing is:

- `syncraft.app.SyncraftApp`
- `syncraft.schemas.ChannelCreate`
- `syncraft.schemas.ChannelUpdate`
- `syncraft.services.storage.test_channel_configuration`
- `syncraft.services.storage.send_channel_sample`
- `syncraft.services.storage.send_channel_message`

Example:

```python
from syncraft.app import SyncraftApp
from syncraft.schemas import ChannelCreate, ChannelUpdate
from syncraft.services.storage import (
    send_channel_message,
    send_channel_sample,
    test_channel_configuration,
)

app = SyncraftApp()

payload = ChannelCreate(
    name="Slack Import Bot",
    platform="slack",
    auth_type="bot",
    auth_config={"values": {"bot_token": "xoxb-..."}},
    target_config={"values": {"channel_id": "C12345678"}},
    provider_account_label="Slack Bot",
    target_label="C12345678",
)

print(app.healthcheck())
print(test_channel_configuration(payload))
print(send_channel_sample(payload, "Syncraft import path says hi"))
print(send_channel_message(payload, content="Syncraft import path says hi"))

created = app.create_channel(payload)
print(app.test_channel_config(payload))
print(app.test_channel(created.id))
print(app.send_channel_sample_message(created.id))
print(app.update_channel(created.id, ChannelUpdate(name="Slack Import Bot Verified")))
print(app.list_channels())
print(app.list_history())
```

Validated against live credentials:

```json
{
  "healthcheck": {
    "status": "ok",
    "environment": "development",
    "database_engine": "sqlite"
  },
  "service_test_channel_configuration": {
    "status": "success",
    "message": "Slack bot token is valid and can post to your_channel_id"
  },
  "service_send_channel_sample": {
    "status": "success",
    "message": "Sample message sent through Slack bot"
  },
  "service_send_channel_message": {
    "status": "success",
    "message": "Sample message sent through Slack bot",
    "asset_count": 0
  }
}
```

Verified `SyncraftApp` channel lifecycle result shape:

```json
{
  "app_test_channel_config": {
    "status": "success",
    "message": "Telegram bot @rilab_studio_bot can reach Syncraft"
  },
  "app_create_channel": {
    "id": "28c88536-82fb-49fc-b0bb-01879c046dc1",
    "name": "Telegram Import Bot",
    "platform": "telegram",
    "auth_type": "bot",
    "status": "active",
    "last_test_status": ""
  },
  "app_test_channel": {
    "id": "28c88536-82fb-49fc-b0bb-01879c046dc1",
    "status": "active",
    "last_test_status": "success"
  },
  "app_send_channel_sample_message": {
    "id": "28c88536-82fb-49fc-b0bb-01879c046dc1",
    "status": "active",
    "last_test_status": "success"
  },
  "app_update_channel": {
    "id": "28c88536-82fb-49fc-b0bb-01879c046dc1",
    "name": "Telegram Import Bot Verified",
    "target_label": "your_channel_id verified",
    "last_test_status": "success"
  }
}
```

## CLI command reference

All command results are printed as JSON.

### Inspect commands and provider support

Show general help:

```sh
syncraft --help
```

Show command-specific help:

```sh
syncraft help templates
```

List supported channel platforms, auth types, and required provider fields:

```sh
syncraft channel-support
```

Example output shape:

```json
[
  {
    "platform": "slack",
    "supported_auth_types": [
      {
        "auth_type": "webhook",
        "required_auth_fields": ["webhook_url"],
        "required_target_fields": [],
        "test_message": "Validated Slack incoming webhook configuration"
      },
      {
        "auth_type": "bot",
        "required_auth_fields": ["bot_token"],
        "required_target_fields": ["channel_id"],
        "test_message": "Validated Slack bot token and channel target shape"
      }
    ]
  }
]
```

### Health

Check runtime health:

```sh
syncraft health
```

Example:

```json
{
  "database_engine": "sqlite",
  "environment": "development",
  "status": "ok"
}
```

### Settings

Read settings:

```sh
syncraft settings get
```

Update settings:

```sh
syncraft settings update --payload '{
  "push_confirmations": true,
  "error_alerts": true,
  "compact_mode": false,
  "keyboard_shortcuts": true,
  "dock_magnification": false,
  "dock_auto_hide": false,
  "channel_test_message": "Syncraft says hi",
  "storage_engine": "local",
  "local_db_path": "/tmp/syncraft.db"
}'
```

### Templates

List templates:

```sh
syncraft templates list
```

Create a template:

```sh
syncraft templates create --payload '{
  "name": "Incident",
  "description": "Reusable alert template",
  "content": "Check service health",
  "icon": "code"
}'
```

Update a template:

```sh
syncraft templates update TEMPLATE_ID --payload '{
  "name": "Incident Updated",
  "content": "Investigate latency and error rate"
}'
```

Delete a template:

```sh
syncraft templates delete TEMPLATE_ID
```

### Variables

List variables:

```sh
syncraft variables list
```

Create a variable:

```sh
syncraft variables create --payload '{
  "name": "service_name",
  "default_value": "billing-api",
  "description": "Default service identifier"
}'
```

Create a variable from file:

```sh
syncraft variables create --payload-file ./variable.json
```

Example `variable.json`:

```json
{
  "name": "service_name",
  "default_value": "billing-api",
  "description": "Default service identifier"
}
```

Update a variable:

```sh
syncraft variables update VARIABLE_ID --payload '{
  "default_value": "auth-api"
}'
```

Delete a variable:

```sh
syncraft variables delete VARIABLE_ID
```

### Channels

List channels:

```sh
syncraft channels list
```

Channel creation requires:

- `name`
- `platform`
- `auth_type`
- `auth_config.values`
- `target_config.values` when the selected provider setup requires it

`capabilities` is optional. If omitted, Syncraft applies the default channel capability set.

Slack incoming webhook channel:

```sh
syncraft channels create --payload '{
  "name": "Slack Alerts",
  "platform": "slack",
  "auth_type": "webhook",
  "auth_config": { "values": { "webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ" } },
  "target_config": { "values": {} },
  "provider_account_label": "Slack Webhook",
  "target_label": "Incoming Webhook"
}'
```

Slack bot channel:

```sh
syncraft channels create --payload '{
  "name": "Slack Ops",
  "platform": "slack",
  "auth_type": "bot",
  "auth_config": { "values": { "bot_token": "xoxb-..." } },
  "target_config": { "values": { "channel_id": "C12345678" } },
  "provider_account_label": "Slack Bot",
  "target_label": "#ops"
}'
```

Verified success response shape:

```json
{
  "id": "22c86e05-99cc-4a8e-8dfa-43c6f4f82886",
  "name": "Slack Alerts Bot",
  "platform": "slack",
  "auth_type": "bot",
  "status": "active",
  "last_test_status": "",
  "provider_account_label": "Slack Bot",
  "target_label": "your_channel_id",
  "auth_config": {
    "values": {
      "bot_token": "<redacted>"
    }
  },
  "target_config": {
    "values": {
      "channel_id": "your_channel_id"
    }
  }
}
```

Telegram bot channel:

```sh
syncraft channels create --payload '{
  "name": "Telegram Alerts",
  "platform": "telegram",
  "auth_type": "bot",
  "auth_config": { "values": { "bot_token": "123456:ABCDEF" } },
  "target_config": { "values": { "chat_id": "-1001234567890" } },
  "provider_account_label": "Telegram Bot",
  "target_label": "Ops Chat"
}'
```

Validated against live credentials:

```sh
syncraft channel-test-config --payload '{ ... }'
```

```json
{
  "status": "success",
  "message": "Telegram bot @rilab_studio_bot can reach Syncraft"
}
```

Test a saved channel:

```sh
syncraft channel-test CHANNEL_ID
```

Slack webhook result:

```json
{
  "last_test_status": "success",
  "status": "active"
}
```

Send a sample message:

```sh
syncraft channel-sample CHANNEL_ID
```

Observed success messages:

```json
{
  "slack_webhook": "Sample message sent to Slack webhook",
  "slack_bot": "Sample message sent through Slack bot",
  "discord_bot": "Message sent through Discord bot",
  "telegram_bot": "Sample message sent through Telegram bot"
}
```

Update a saved channel:

```sh
syncraft channels update CHANNEL_ID --payload '{
  "name": "Slack Alerts Bot Verified",
  "target_label": "your_channel_id verified"
}'
```

Verified update response shape:

```json
{
  "id": "22c86e05-99cc-4a8e-8dfa-43c6f4f82886",
  "name": "Slack Alerts Bot Verified",
  "platform": "slack",
  "auth_type": "bot",
  "status": "active",
  "last_test_status": "success",
  "target_label": "your_channel_id verified"
}
```

Update a channel:

```sh
syncraft channels update CHANNEL_ID --payload '{
  "name": "Slack Ops Primary",
  "target_label": "#ops-primary"
}'
```

Update a bot token and channel destination:

```sh
syncraft channels update CHANNEL_ID --payload '{
  "auth_config": { "values": { "bot_token": "xoxb-new-token" } },
  "target_config": { "values": { "channel_id": "C99999999" } },
  "provider_account_label": "Slack Bot",
  "target_label": "#ops-primary"
}'
```

Update a webhook URL:

```sh
syncraft channels update CHANNEL_ID --payload '{
  "auth_config": { "values": { "webhook_url": "https://hooks.slack.com/services/NEW/WEBHOOK/URL" } }
}'
```

Delete a channel:

```sh
syncraft channels delete CHANNEL_ID
```

Validate a channel configuration without saving it:

```sh
syncraft channel-test-config --payload '{
  "name": "Slack Test",
  "platform": "slack",
  "auth_type": "webhook",
  "auth_config": { "values": { "webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ" } },
  "target_config": { "values": {} }
}'
```

Test an existing saved channel:

```sh
syncraft channel-test CHANNEL_ID
```

Send a sample message to an existing saved channel:

```sh
syncraft channel-sample CHANNEL_ID
```

## Python usage

```python
from syncraft.app import SyncraftApp

app = SyncraftApp()
print(app.healthcheck())
print(app.list_templates())

template = app.create_template(
    {
        "name": "Incident",
        "description": "Reusable alert template",
        "content": "Check service health",
        "icon": "code",
    }
)
print(template.id)
```

## Configuration

Syncraft now prefers `SYNCRAFT_*` environment variables:

```sh
SYNCRAFT_ENV=production
SYNCRAFT_DATABASE_ENGINE=sqlite
SYNCRAFT_DATABASE_URL=sqlite:////tmp/syncraft.db
SYNCRAFT_APP_NAME=Syncraft
```

Default local storage is SQLite under the Syncraft application data directory.

## License

Syncraft is released under the MIT License.

See [LICENSE](LICENSE) for the full text.
