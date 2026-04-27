CREATE TABLE app_settings (
    id INTEGER PRIMARY KEY,
    push_confirmations BOOLEAN NOT NULL DEFAULT 1,
    error_alerts BOOLEAN NOT NULL DEFAULT 1,
    compact_mode BOOLEAN NOT NULL DEFAULT 0,
    keyboard_shortcuts BOOLEAN NOT NULL DEFAULT 1,
    dock_magnification BOOLEAN NOT NULL DEFAULT 0,
    dock_auto_hide BOOLEAN NOT NULL DEFAULT 0,
    channel_test_message TEXT NOT NULL DEFAULT 'Pulsar says Hi !!!',
    storage_engine VARCHAR(32) NOT NULL DEFAULT 'local',
    db_host VARCHAR(255) NOT NULL DEFAULT '',
    db_port VARCHAR(32) NOT NULL DEFAULT '',
    db_database VARCHAR(255) NOT NULL DEFAULT '',
    db_username VARCHAR(255) NOT NULL DEFAULT '',
    db_password VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE channels (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    platform VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    auth_type VARCHAR(32) NOT NULL,
    credentials TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    channel_identifier VARCHAR(255),
    auth_config_json TEXT,
    target_config_json TEXT,
    provider_account_label VARCHAR(255) NOT NULL DEFAULT '',
    target_label VARCHAR(255) NOT NULL DEFAULT '',
    setup_error TEXT NOT NULL DEFAULT '',
    last_test_status VARCHAR(32) NOT NULL DEFAULT 'untested',
    last_checked DATETIME,
    asset_sync_cursor TEXT NOT NULL DEFAULT '',
    asset_last_synced_at DATETIME,
    message_sync_cursor TEXT NOT NULL DEFAULT '',
    message_last_synced_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE templates (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    icon VARCHAR(32) NOT NULL DEFAULT 'code',
    language VARCHAR(64),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE variables (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    default_value TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE api_keys (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    provider VARCHAR(128) NOT NULL,
    secret_value TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE assets (
    id VARCHAR(36) PRIMARY KEY,
    channel_id VARCHAR(36),
    platform VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    asset_type VARCHAR(32) NOT NULL,
    mime_type VARCHAR(255) NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL,
    provider_asset_id VARCHAR(255) NOT NULL DEFAULT '',
    provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
    remote_url TEXT NOT NULL DEFAULT '',
    preview_url TEXT NOT NULL DEFAULT '',
    mirror_group_id VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'synced',
    tags_json TEXT NOT NULL DEFAULT '[]',
    mirrors INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE SET NULL
);

CREATE TABLE activity_logs (
    id VARCHAR(36) PRIMARY KEY,
    category VARCHAR(32) NOT NULL,
    action VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'info',
    title VARCHAR(255) NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    entity_type VARCHAR(32) NOT NULL DEFAULT '',
    entity_id VARCHAR(64) NOT NULL DEFAULT '',
    channel_id VARCHAR(36),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE SET NULL
);

CREATE TABLE channel_messages (
    id VARCHAR(36) PRIMARY KEY,
    channel_id VARCHAR(36),
    platform VARCHAR(64) NOT NULL,
    provider_chat_id VARCHAR(255) NOT NULL DEFAULT '',
    provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
    author_name VARCHAR(255) NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    attachment_count INTEGER NOT NULL DEFAULT 0,
    external_url TEXT NOT NULL DEFAULT '',
    sent_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE SET NULL
);

CREATE TABLE webhooks (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    target_url TEXT NOT NULL,
    event_scope VARCHAR(32) NOT NULL DEFAULT 'app',
    http_method VARCHAR(16) NOT NULL DEFAULT 'POST',
    secret TEXT NOT NULL DEFAULT '',
    is_enabled BOOLEAN NOT NULL DEFAULT 1,
    channel_id VARCHAR(36),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);
