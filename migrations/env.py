"""Alembic environment for control-plane migrations only."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from medical_ai.config import get_settings
from medical_ai.identity import IDENTITY_TABLE_NAMES, identity_metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = identity_metadata


def include_object(obj, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
    """Keep analytics objects outside control-plane autogeneration."""

    if type_ == "table":
        return bool(name in IDENTITY_TABLE_NAMES)
    table = getattr(obj, "table", None)
    if table is not None:
        return bool(table.name in IDENTITY_TABLE_NAMES)
    return True


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply control-plane revisions through a short-lived engine."""

    # Build directly from the URL object so a plaintext password is never
    # copied into Alembic's string configuration or diagnostic rendering.
    connectable = create_engine(
        get_settings().mysql_url(),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
