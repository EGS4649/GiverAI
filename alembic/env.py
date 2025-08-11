import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 1. Add this so Alembic can see your app folder
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/..')

# 2. Import your Base from main.py
from main import Base, DATABASE_URL

# Alembic config
config = context.config

# Interpret config file for Python logging.
fileConfig(config.config_file_name)

# Set target metadata for 'autogenerate' feature
target_metadata = Base.metadata

# Replace sqlalchemy.url in alembic.ini dynamically
config.set_main_option("sqlalchemy.url", DATABASE_URL)

def run_migrations_offline():
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
