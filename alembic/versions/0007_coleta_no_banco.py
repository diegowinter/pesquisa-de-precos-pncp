"""coleta da etapa 2 no banco: progresso, pendentes e identificadores do PNCP (Fase 10, bloco C)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20

Três mudanças, todas para a etapa 2 rodar sem tocar em disco.

1. `documento.numero_sequencial` / `numero_sequencial_ata` — BURACO do schema original, não
   escolha nova. A etapa 2 produz os dois desde a Fase 8 (ADR-011/012: a 5 baixa o PDF depois
   do corte e precisa refazer `listar_arquivos()` sem reconsultar a busca), mas
   docs/02_SCHEMA.md é anterior a essa mudança e a tabela nunca ganhou as colunas. Migrar a
   etapa 2 sem elas faria o caminho `--fonte banco` perder silenciosamente a informação que a
   etapa 5 precisa — e o sintoma só apareceria dois blocos adiante.

2. `coleta_progresso` — espelho de `checkpoints/2_progresso.csv`: quais (termo, tipo_doc) já
   foram varridos. Chaveado por `termo_id`, não pelo texto do termo: o texto pode ser
   reescrito pela curadoria, o id não.

   Diferente das outras etapas, aqui o checkpoint NÃO é derivável do resultado. "Já varri este
   termo" não é o mesmo que "este termo trouxe documento": uma busca legítima pode não
   retornar nada, e derivar do resultado faria a etapa revarrer esses termos para sempre.

3. `coleta_pendente` — espelho de `checkpoints/2_pendentes.csv`: documentos que apareceram na
   busca mas ainda não tinham resultado homologado. A homologação sai depois, e o `--atualizar`
   revisita esta lista antes de mais nada. `base` é `jsonb` porque é exatamente o dict que
   `coleta_pncp.revisitar_pendente()` recebe de volta — serializá-lo em colunas seria
   reimplementar o parser da API por fora dele.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
ALTER TABLE documento ADD COLUMN numero_sequencial text;
ALTER TABLE documento ADD COLUMN numero_sequencial_ata text;

CREATE TABLE coleta_progresso (
    termo_id         bigint NOT NULL REFERENCES termo(id) ON DELETE CASCADE,
    tipo_doc         tipo_documento NOT NULL,
    n_documentos     integer NOT NULL DEFAULT 0,
    n_itens          integer NOT NULL DEFAULT 0,
    concluido_em     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (termo_id, tipo_doc)
);

CREATE TABLE coleta_pendente (
    numero_controle_pncp text PRIMARY KEY,
    tipo_doc         tipo_documento NOT NULL,
    termo_id         bigint REFERENCES termo(id) ON DELETE SET NULL,
    motivo           text NOT NULL DEFAULT 'sem_homologado',
    data             text,
    base             jsonb NOT NULL,
    visto_em         timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS coleta_pendente;"
        "DROP TABLE IF EXISTS coleta_progresso;"
        "ALTER TABLE documento DROP COLUMN IF EXISTS numero_sequencial;"
        "ALTER TABLE documento DROP COLUMN IF EXISTS numero_sequencial_ata;"
    )
