from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from shutil import copy2
from uuid import uuid4
from urllib.parse import quote_plus

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from syncraft.config import AppConfig


Base = declarative_base()
SUPPORTED_SQL_ENGINES = {"sqlite", "postgresql", "mysql"}
SUPPORTED_STORAGE_ENGINES = SUPPORTED_SQL_ENGINES | {"mongodb"}
SQLALCHEMY_DRIVERS = {
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
}


def build_engine(config: AppConfig):
    if config.database_engine not in SUPPORTED_SQL_ENGINES:
        raise ValueError(
            f"Unsupported database engine '{config.database_engine}'. "
            f"Supported engines: {', '.join(sorted(SUPPORTED_SQL_ENGINES))}."
        )

    engine_options: dict[str, object] = {"future": True}
    if config.database_engine == "sqlite":
        sqlite_path = config.database_url.removeprefix("sqlite:///")
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        engine_options["connect_args"] = {"check_same_thread": False}
    else:
        engine_options["pool_pre_ping"] = True
    return create_engine(config.database_url, **engine_options)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


def sqlite_path_from_url(database_url: str) -> Path:
    return Path(database_url.removeprefix("sqlite:///")).expanduser().resolve()


def write_local_db_pointer(config: AppConfig, database_path: Path) -> None:
    config.local_db_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    config.local_db_pointer_path.write_text(str(database_path), encoding="utf-8")


class DatabaseManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.engine: Engine | None = None
        self.session_factory: sessionmaker[Session] | None = None
        self.mongo_client = None
        self.mongo_database = None
        self._configure(config)

    def switch_database_url(self, database_url: str) -> None:
        next_config = replace(self.config, database_url=database_url)
        self.switch_config(next_config)

    def switch_config(self, next_config: AppConfig) -> None:
        self.dispose()
        self._configure(next_config)

    def relocate_local_database(self, location: str) -> str:
        if self.config.database_engine != "sqlite":
            raise ValueError("Local database relocation is only supported for sqlite")
        current_path = sqlite_path_from_url(self.config.database_url)
        requested_path = Path(location).expanduser().resolve()
        target_path = (
            requested_path
            if requested_path.suffix.lower() == ".db"
            else requested_path / current_path.name
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if current_path != target_path:
            copy2(current_path, target_path)
        write_local_db_pointer(self.config, target_path)
        self.switch_database_url(f"sqlite:///{target_path}")
        return str(target_path)

    def sync_to_database(self, target_config: AppConfig) -> None:
        if target_config.database_engine not in SUPPORTED_STORAGE_ENGINES:
            raise ValueError(
                f"Unsupported sync target '{target_config.database_engine}'. "
                f"Supported engines: {', '.join(sorted(SUPPORTED_STORAGE_ENGINES))}."
            )
        if self.config.database_engine in SUPPORTED_SQL_ENGINES and target_config.database_engine in SUPPORTED_SQL_ENGINES:
            ensure_database_exists(target_config)
            target_engine = build_engine(target_config)
            try:
                ensure_runtime_schema(target_engine)
                Base.metadata.create_all(bind=target_engine)
                sync_engines(self.engine, target_engine)
            finally:
                target_engine.dispose()
            return
        if self.config.database_engine in SUPPORTED_SQL_ENGINES and target_config.database_engine == "mongodb":
            _, target_database = build_mongo_database(target_config)
            try:
                sync_sql_to_mongo(self.engine, target_database)
            finally:
                target_database.client.close()
            return
        if self.config.database_engine == "mongodb" and target_config.database_engine in SUPPORTED_SQL_ENGINES:
            ensure_database_exists(target_config)
            target_engine = build_engine(target_config)
            try:
                ensure_runtime_schema(target_engine)
                Base.metadata.create_all(bind=target_engine)
                sync_mongo_to_sql(self.mongo_database, target_engine)
            finally:
                target_engine.dispose()
            return
        if self.config.database_engine == "mongodb" and target_config.database_engine == "mongodb":
            _, target_database = build_mongo_database(target_config)
            try:
                sync_mongo_to_mongo(self.mongo_database, target_database)
            finally:
                target_database.client.close()
            return
        raise ValueError(
            f"Unsupported sync path '{self.config.database_engine}' -> '{target_config.database_engine}'."
        )

    def dispose(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
        if self.mongo_client is not None:
            self.mongo_client.close()
        self.engine = None
        self.session_factory = None
        self.mongo_client = None
        self.mongo_database = None

    def _configure(self, config: AppConfig) -> None:
        self.config = config
        if config.database_engine in SUPPORTED_SQL_ENGINES:
            ensure_database_exists(config)
            engine = build_engine(config)
            session_factory = build_session_factory(engine)
            self.engine = engine
            self.session_factory = session_factory
            ensure_runtime_schema(engine)
            Base.metadata.create_all(bind=engine)
            return
        if config.database_engine == "mongodb":
            client, database = build_mongo_database(config)
            self.mongo_client = client
            self.mongo_database = database
            ensure_mongo_schema(database)
            return
        raise ValueError(
            f"Unsupported database engine '{config.database_engine}'. "
            f"Supported engines: {', '.join(sorted(SUPPORTED_STORAGE_ENGINES))}."
        )


def build_database_url(engine: str, *, host: str = "", port: str = "", database: str = "", username: str = "", password: str = "") -> str:
    if engine == "sqlite":
        raise ValueError("SQLite database URLs should be supplied directly.")
    if engine not in SUPPORTED_STORAGE_ENGINES:
        raise ValueError(f"Unsupported database engine '{engine}'.")
    if not host.strip() or not database.strip():
        raise ValueError(f"{engine} configuration requires both host and database.")

    encoded_username = quote_plus(username) if username else ""
    encoded_password = quote_plus(password) if password else ""
    auth_segment = ""
    if encoded_username:
        auth_segment = encoded_username
        if encoded_password:
            auth_segment = f"{auth_segment}:{encoded_password}"
        auth_segment = f"{auth_segment}@"

    host_segment = host.strip()
    if port.strip():
        host_segment = f"{host_segment}:{port.strip()}"
    if engine == "mongodb":
        return f"mongodb://{auth_segment}{host_segment}/{database.strip()}"
    dialect = SQLALCHEMY_DRIVERS.get(engine, engine)
    return f"{dialect}://{auth_segment}{host_segment}/{database.strip()}"


def ensure_database_exists(config: AppConfig) -> None:
    if config.database_engine == "sqlite":
        return
    url = make_url(config.database_url)
    database_name = url.database
    if not database_name:
        raise ValueError(f"{config.database_engine} configuration requires a database name.")

    admin_url = _admin_database_url(config.database_engine, url)
    admin_engine = create_engine(admin_url, future=True, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin_engine.connect() as connection:
            if config.database_engine == "postgresql":
                exists = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                ).scalar()
                if not exists:
                    escaped_name = database_name.replace('"', '""')
                    connection.execute(text(f'CREATE DATABASE "{escaped_name}"'))
            elif config.database_engine == "mysql":
                connection.execute(text(f"CREATE DATABASE IF NOT EXISTS `{database_name.replace('`', '``')}`"))
    finally:
        admin_engine.dispose()


def _admin_database_url(engine: str, url: URL) -> URL:
    if engine == "postgresql":
        return url.set(database="postgres")
    if engine == "mysql":
        return url.set(database="")
    return url


def sync_engines(source_engine: Engine, target_engine: Engine) -> None:
    sync_tables = _sync_tables()
    channel_model = sync_tables[1]
    source_session_factory = build_session_factory(source_engine)
    target_session_factory = build_session_factory(target_engine)
    with source_session_factory() as source_session, target_session_factory() as target_session:
        channel_ids = {
            channel_id
            for (channel_id,) in source_session.query(channel_model.id).all()
        }
        for model in sync_tables:
            for row in source_session.query(model).all():
                target_session.merge(_clone_model_instance(model, row, channel_ids))
            target_session.commit()


def _clone_model_instance(model, instance, channel_ids: set[str]):
    values = {
        column.name: getattr(instance, column.name)
        for column in model.__table__.columns
    }
    if "channel_id" in values and values["channel_id"] and values["channel_id"] not in channel_ids:
        values["channel_id"] = None
    return model(**values)


def build_mongo_database(config: AppConfig):
    from pymongo import MongoClient

    client = MongoClient(config.database_url)
    database_name = mongo_database_name(config.database_url)
    if not database_name:
        raise ValueError("mongodb configuration requires a database name.")
    return client, client[database_name]


def mongo_database_name(database_url: str) -> str:
    path = database_url.split("?", 1)[0].rsplit("/", 1)[-1]
    return path.strip()


def ensure_mongo_schema(database) -> None:
    database["app_settings"].create_index("id", unique=True)
    database["channels"].create_index("id", unique=True)
    database["templates"].create_index("id", unique=True)
    database["variables"].create_index("id", unique=True)
    database["variables"].create_index("name", unique=True)
    database["api_keys"].create_index("id", unique=True)
    database["assets"].create_index("id", unique=True)
    database["channel_messages"].create_index("id", unique=True)
    database["activity_logs"].create_index("id", unique=True)
    database["webhooks"].create_index("id", unique=True)
    database["assets"].create_index([("mirror_group_id", 1)])
    database["channel_messages"].create_index([("platform", 1), ("provider_message_id", 1)])


def sync_sql_to_mongo(source_engine: Engine, target_database) -> None:
    ensure_mongo_schema(target_database)
    source_session_factory = build_session_factory(source_engine)
    sync_tables = _sync_tables()
    channel_model = sync_tables[1]
    with source_session_factory() as source_session:
        channel_ids = {channel_id for (channel_id,) in source_session.query(channel_model.id).all()}
        for model in sync_tables:
            for row in source_session.query(model).all():
                document = _sync_document_for_model(model, _sync_values_for_instance(model, row, channel_ids))
                target_database[model.__tablename__].replace_one({"_id": document["_id"]}, document, upsert=True)


def sync_mongo_to_sql(source_database, target_engine: Engine) -> None:
    sync_tables = _sync_tables()
    channel_ids = {
        document["id"]
        for document in source_database["channels"].find({}, {"id": 1})
        if document.get("id")
    }
    target_session_factory = build_session_factory(target_engine)
    with target_session_factory() as target_session:
        for model in sync_tables:
            for document in source_database[model.__tablename__].find():
                values = _sync_values_for_document(model, document, channel_ids)
                target_session.merge(model(**values))
            target_session.commit()


def sync_mongo_to_mongo(source_database, target_database) -> None:
    ensure_mongo_schema(target_database)
    for model in _sync_tables():
        for document in source_database[model.__tablename__].find():
            payload = dict(document)
            target_database[model.__tablename__].replace_one({"_id": payload["_id"]}, payload, upsert=True)


def _sync_values_for_instance(model, instance, channel_ids: set[str]):
    values = {
        column.name: getattr(instance, column.name)
        for column in model.__table__.columns
    }
    if "channel_id" in values and values["channel_id"] and values["channel_id"] not in channel_ids:
        values["channel_id"] = None
    return values


def _sync_values_for_document(model, document: dict, channel_ids: set[str]):
    values = {
        column.name: document.get(column.name)
        for column in model.__table__.columns
        if column.name in document
    }
    if "channel_id" in values and values["channel_id"] and values["channel_id"] not in channel_ids:
        values["channel_id"] = None
    return values


def _sync_document_for_model(model, values: dict):
    document = dict(values)
    document["_id"] = values.get("id") or uuid4().hex
    if "id" in values and not values.get("id"):
        document["id"] = document["_id"]
    return document


def _sync_tables():
    from syncraft import models

    return (
        models.AppSettings,
        models.Channel,
        models.Template,
        models.Variable,
        models.ApiKey,
        models.Asset,
        models.ChannelMessage,
        models.ActivityLog,
        models.Webhook,
    )


def ensure_runtime_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    if "channels" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("channels")}
    alter_statements = {
        "auth_config_json": "ALTER TABLE channels ADD COLUMN auth_config_json TEXT",
        "target_config_json": "ALTER TABLE channels ADD COLUMN target_config_json TEXT",
        "provider_account_label": "ALTER TABLE channels ADD COLUMN provider_account_label VARCHAR(255) NOT NULL DEFAULT ''",
        "target_label": "ALTER TABLE channels ADD COLUMN target_label VARCHAR(255) NOT NULL DEFAULT ''",
        "setup_error": "ALTER TABLE channels ADD COLUMN setup_error TEXT NOT NULL DEFAULT ''",
        "last_test_status": "ALTER TABLE channels ADD COLUMN last_test_status VARCHAR(32) NOT NULL DEFAULT ''",
        "asset_sync_cursor": "ALTER TABLE channels ADD COLUMN asset_sync_cursor TEXT NOT NULL DEFAULT ''",
        "asset_last_synced_at": "ALTER TABLE channels ADD COLUMN asset_last_synced_at DATETIME",
        "message_sync_cursor": "ALTER TABLE channels ADD COLUMN message_sync_cursor TEXT NOT NULL DEFAULT ''",
        "message_last_synced_at": "ALTER TABLE channels ADD COLUMN message_last_synced_at DATETIME",
    }
    with engine.begin() as connection:
        for column, statement in alter_statements.items():
            if column not in existing_columns:
                connection.execute(text(statement))

    if "app_settings" in inspector.get_table_names():
        settings_columns = {column["name"] for column in inspector.get_columns("app_settings")}
        if "channel_test_message" not in settings_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE app_settings ADD COLUMN channel_test_message TEXT NOT NULL DEFAULT 'Syncraft says hi'"
                    )
                )
        if "local_db_directory" not in settings_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE app_settings ADD COLUMN local_db_directory VARCHAR(1024) NOT NULL DEFAULT ''"
                    )
                )

    if "assets" in inspector.get_table_names():
        asset_columns = {column["name"] for column in inspector.get_columns("assets")}
        with engine.begin() as connection:
            if "mirror_group_id" not in asset_columns:
                connection.execute(
                    text("ALTER TABLE assets ADD COLUMN mirror_group_id VARCHAR(64) NOT NULL DEFAULT ''")
                )
                asset_columns.add("mirror_group_id")
        expected_columns = {
            "id",
            "channel_id",
            "platform",
            "name",
            "asset_type",
            "mime_type",
            "size_bytes",
            "provider_asset_id",
            "provider_message_id",
            "remote_url",
            "preview_url",
            "mirror_group_id",
            "status",
            "tags_json",
            "mirrors",
            "created_at",
            "updated_at",
        }
        if not expected_columns.issubset(asset_columns):
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE assets"))

    if "activity_logs" in inspector.get_table_names():
        activity_columns = {column["name"] for column in inspector.get_columns("activity_logs")}
        expected_columns = {
            "id",
            "category",
            "action",
            "status",
            "title",
            "detail",
            "entity_type",
            "entity_id",
            "channel_id",
            "metadata_json",
            "created_at",
        }
        if not expected_columns.issubset(activity_columns):
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE activity_logs"))

    if "channel_messages" in inspector.get_table_names():
        message_columns = {column["name"] for column in inspector.get_columns("channel_messages")}
        with engine.begin() as connection:
            if "provider_chat_id" not in message_columns:
                connection.execute(
                    text("ALTER TABLE channel_messages ADD COLUMN provider_chat_id VARCHAR(255) NOT NULL DEFAULT ''")
                )
                message_columns.add("provider_chat_id")
        expected_columns = {
            "id",
            "channel_id",
            "platform",
            "provider_chat_id",
            "provider_message_id",
            "author_name",
            "content",
            "attachment_count",
            "external_url",
            "sent_at",
            "created_at",
            "updated_at",
        }
        if not expected_columns.issubset(message_columns):
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE channel_messages"))


def get_db(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
