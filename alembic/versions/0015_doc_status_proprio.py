"""`item_enriquecido.doc_status` guarda o veredito da etapa 5, nao o estado do documento

A coluna nasceu com o tipo `estado_documento`, que e o enum da FILA (`descoberto`,
`baixando`, `extraido`...). Mas `doc_status` e outra coisa: e o veredito de QUALIDADE da
extracao (`ok`, `fora_de_escopo`, `ilegivel`). Os dois vocabularios so se parecem.

O sintoma foi um erro de COPY em 2026-08-29 — `valor de entrada e invalido para enum
estado_documento: "ok"` — e o conserto de entao foi errado: envolveu o valor em
`estado_documento()`, que ACHATA `ok` e `fora_de_escopo` em `extraido`. Resultado: 2.545
documentos gravados, 100% deles com `doc_status = 'extraido'`, e a coluna que existia para
responder "a extracao deu bom resultado?" respondendo sempre a mesma coisa.

O veredito nao se perdeu de vez porque da para reconstrui-lo dos itens — `ok` e o documento
que tem ao menos um `pdf_ok%` —, e e exatamente isso que o backfill faz.

`suspeito` NAO entra no enum novo: a etapa 5 deixou de produzi-lo em 2026-08-29 (ele era o
detector de PDF trocado, e a ADR-024 mostrou que o sinal que o alimentava era duplicacao).

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UP = """
CREATE TYPE doc_status AS ENUM ('ok', 'fora_de_escopo', 'ilegivel');

ALTER TABLE item_enriquecido ALTER COLUMN doc_status TYPE text;

-- Backfill: o veredito reconstruido dos itens do proprio documento. `ilegivel` e o documento
-- de que nao saiu tabela — todos os itens dele ficaram em `sem_texto`.
UPDATE item_enriquecido e SET doc_status = v.veredito
  FROM (SELECT numero_controle_pncp,
               CASE WHEN bool_or(status::text LIKE 'pdf_ok%') THEN 'ok'
                    WHEN bool_and(status::text = 'sem_texto') THEN 'ilegivel'
                    ELSE 'fora_de_escopo' END AS veredito
          FROM item_enriquecido GROUP BY 1) v
 WHERE v.numero_controle_pncp = e.numero_controle_pncp;

ALTER TABLE item_enriquecido
  ALTER COLUMN doc_status TYPE doc_status USING doc_status::doc_status;
"""

# O caminho de volta achata de novo — e o achatamento e justamente o defeito que esta
# migration remove. `ok` e `fora_de_escopo` sao dois documentos extraidos.
DOWN = """
ALTER TABLE item_enriquecido ALTER COLUMN doc_status TYPE text;
UPDATE item_enriquecido
   SET doc_status = CASE WHEN doc_status = 'ilegivel' THEN 'ilegivel' ELSE 'extraido' END;
ALTER TABLE item_enriquecido
  ALTER COLUMN doc_status TYPE estado_documento USING doc_status::estado_documento;
DROP TYPE IF EXISTS doc_status;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
