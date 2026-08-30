"""`item_error.last_seen` — quando o erro aconteceu pela ULTIMA vez

`created_at` guarda a PRIMEIRA ocorrencia e nunca e atualizado, porque `registrar_erro_item`
reaproveita a linha por `(run_id, step, key)` em vez de acumular duplicata. Como todas as
execucoes de uma etapa compartilham o mesmo `run_id`, um documento que falha de novo so
incrementa `attempts` — a linha continua datada de dias atras.

Efeito pratico (2026-08-30): a tela marcava "erros: 21" e a consulta
`WHERE created_at > now() - interval '30 min'` devolvia ZERO linhas. Por alguns minutos
pareceu que a gravacao de erro estava quebrada; nao estava, era so impossivel datar a ultima
ocorrencia.

Backfill: `created_at`, que e a unica data conhecida das linhas antigas.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE item_error ADD COLUMN IF NOT EXISTS last_seen timestamptz;
UPDATE item_error SET last_seen = created_at WHERE last_seen IS NULL;
ALTER TABLE item_error ALTER COLUMN last_seen SET DEFAULT now();
ALTER TABLE item_error ALTER COLUMN last_seen SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_erro_last_seen ON item_error (step, last_seen DESC);
"""

DOWN = """
DROP INDEX IF EXISTS ix_erro_last_seen;
ALTER TABLE item_error DROP COLUMN IF EXISTS last_seen;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
