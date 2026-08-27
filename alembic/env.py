import importlib
from logging.config import fileConfig
from typing import Any, cast

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from recover_net.db.session import DATABASE_URL, Base

load_dotenv()

importlib.import_module("recover_net.db.models")

alembic_context = cast(Any, importlib.import_module("alembic.context"))
config = alembic_context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    alembic_context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with alembic_context.begin_transaction():
        alembic_context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        alembic_context.configure(connection=connection, target_metadata=target_metadata)
        with alembic_context.begin_transaction():
            alembic_context.run_migrations()


if alembic_context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
