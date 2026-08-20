"""capacidade ganha 'pdf' e 'pareamento' (Fase 11, ADR-019)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-20

Dois valores novos no enum `capacidade`, para que `provedor`/`capacidade_provedor` possam
apontar quem atende o parse de PDF e o pareamento — como já fazem com chat/embed/rerank/ocr.

`ADD VALUE IF NOT EXISTS` e não um `CREATE TYPE` novo: recriar o tipo exigiria reescrever
todas as colunas que o usam. O preço é que **não há downgrade real** — o PostgreSQL não
remove valor de enum. O downgrade é no-op declarado, não esquecimento: um valor a mais no
enum é inerte enquanto nenhuma linha o usar.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fora de transação: ALTER TYPE ... ADD VALUE não pode rodar dentro de um bloco
    # transacional em versões antigas do PG, e o autocommit aqui é inofensivo (é DDL idempotente).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE capacidade ADD VALUE IF NOT EXISTS 'pdf'")
        op.execute("ALTER TYPE capacidade ADD VALUE IF NOT EXISTS 'pareamento'")


def downgrade() -> None:
    """No-op: PostgreSQL não remove valor de enum. Ver docstring do módulo."""
