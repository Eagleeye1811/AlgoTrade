from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.database.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def sync_url() -> str:
    url = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url
    return url.replace("+asyncpg", "+psycopg2")


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in (None, "paper_broker")
    return True


def run_migrations_offline() -> None:
    context.configure(url=sync_url(), target_metadata=target_metadata, literal_binds=True, include_schemas=True,
                      include_name=include_name)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS paper_broker")
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata, include_schemas=True,
                          include_name=include_name)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
