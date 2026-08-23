"""
Ambiente do Alembic.

A URL de conexão vem de `db.session.database_url()` (ou seja, do `DATABASE_URL` do `.env`), nunca
do `alembic.ini` — um só lugar para apontar o banco.

`target_metadata` existe para o `--autogenerate` conseguir diffar, mas a migration inicial é
escrita à MÃO com o DDL de docs/02_SCHEMA.md, que é normativo até o nome do índice parcial. O
autogenerate não reproduz `WHERE ativo`, `USING gin` nem colunas geradas de forma confiável;
usá-lo ali produziria um schema parecido, não o schema.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from pesquisa_precos.db.models import Base
from pesquisa_precos.db.session import database_url

config = context.config
config.set_main_option("sqlalchemy.url", database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
