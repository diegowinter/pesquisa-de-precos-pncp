"""notificacao_destinatario — CRUD de quem recebe notificação (Fase 9, e-mail via Resend)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17

Tabela nova, fora do DDL original de docs/02_SCHEMA.md (que é anterior a esta feature). Guarda
só QUEM recebe (nome, e-mail, chat_id do Telegram) — credenciais (API key do Resend, token do
bot) continuam só em `.env`, nunca no banco (docs/07_DECISOES.md ADR-006).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE notificacao_destinatario (
    id               bigserial PRIMARY KEY,
    nome             text,
    email            text,
    telegram_chat_id text,
    ativo            boolean NOT NULL DEFAULT true,
    criado_em        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_notificacao_destinatario_tem_canal
        CHECK (email IS NOT NULL OR telegram_chat_id IS NOT NULL)
);
CREATE INDEX ix_notificacao_destinatario_ativo
    ON notificacao_destinatario (ativo) WHERE ativo;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notificacao_destinatario;")
