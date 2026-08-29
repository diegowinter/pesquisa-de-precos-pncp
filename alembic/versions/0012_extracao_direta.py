"""extracao direta da tabela de itens (ADR-023)

A etapa 5 deixa de transcrever o documento inteiro e passa a pedir ao modelo, com o PDF
anexo, so a TABELA DE ITENS — em texto livre, "as it is". Consequencias no schema:

  documento_pagina        DROPADA. Era o gigante do banco (888 mil linhas, 2,6 GB de texto
                          por pagina) e ninguem a jusante a lia: as etapas 6 a 8 so leem
                          item_enriquecido.descricao_final e .destino.
  documento_extracao      1 linha por documento (era 1 por documento+estrategia). Ganha
                          `tabela_texto`; perde `estrategia`, `itens_json` e `n_paginas_ocr`.
  item_enriquecido        perde `estrategia`.
  extraction_strategy     DROPADO — nao existe mais estrategia plugavel.
  capability              'pdf' vira 'extract'. Nao e so nome: 'pdf' era o servico HTTP do
                          companion (parse + rasterizacao + OCR); 'extract' e um LLM
                          multimodal, cadastrado com base_url/modelo/chave como o chat.
                          'ocr' sai junto — sem cliente desde a ADR-021.

O downgrade recria as estruturas VAZIAS. O texto por pagina e a coluna `estrategia` nao
voltam: o dado nao existe mais em lugar nenhum depois do upgrade, e fingir que volta seria
pior que assumir a perda. Quem precisar do acervo antigo tem `../pipeline-csv-congelado/`.

Revision ID: 0012
Revises: 0011
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UP = """
DROP TABLE IF EXISTS documento_pagina;

ALTER TABLE documento_extracao
    DROP CONSTRAINT IF EXISTS documento_extracao_numero_controle_pncp_estrategia_key,
    DROP COLUMN IF EXISTS estrategia,
    DROP COLUMN IF EXISTS itens_json,
    DROP COLUMN IF EXISTS n_paginas_ocr,
    ADD COLUMN IF NOT EXISTS tabela_texto text NOT NULL DEFAULT '';

-- 1 linha por documento: sem `estrategia` no par, o mesmo documento extraido de novo TEM de
-- sobrescrever. Duplicatas herdadas (o mesmo doc por duas estrategias) sao colapsadas na
-- linha mais recente antes de criar a restricao — senao o ADD UNIQUE falha.
DELETE FROM documento_extracao a
 USING documento_extracao b
 WHERE a.numero_controle_pncp = b.numero_controle_pncp AND a.id < b.id;

ALTER TABLE documento_extracao
    ADD CONSTRAINT documento_extracao_numero_controle_pncp_key
        UNIQUE (numero_controle_pncp);

ALTER TABLE item_enriquecido DROP COLUMN IF EXISTS estrategia;

DROP TYPE IF EXISTS extraction_strategy;

ALTER TYPE capability RENAME VALUE 'pdf' TO 'extract';
"""

# `ocr` some do enum `capability` numa etapa a parte: o PostgreSQL nao remove label de enum,
# so recria o tipo. Quatro colunas dependem dele — `provider.capabilities` e um ARRAY (cast em
# duas etapas, `::text[]::capability[]`) e `prompt.capability` tem DEFAULT, que o PG se recusa
# a converter junto: os dois defaults saem antes e voltam depois.
UP_SEM_OCR = """
ALTER TYPE capability RENAME TO capability_antigo;
CREATE TYPE capability AS ENUM ('chat', 'embed', 'rerank', 'extract', 'matching');
ALTER TABLE provider ALTER COLUMN capabilities DROP DEFAULT;
ALTER TABLE prompt ALTER COLUMN capability DROP DEFAULT;
ALTER TABLE prompt ALTER COLUMN capability DROP DEFAULT;
ALTER TABLE provider_capability
    ALTER COLUMN capability TYPE capability USING capability::text::capability;
ALTER TABLE provider
    ALTER COLUMN capabilities TYPE capability[] USING capabilities::text[]::capability[];
ALTER TABLE prompt
    ALTER COLUMN capability TYPE capability USING capability::text::capability;
ALTER TABLE llm_call
    ALTER COLUMN capability TYPE capability USING capability::text::capability;
ALTER TABLE provider ALTER COLUMN capabilities SET DEFAULT '{}'::capability[];
ALTER TABLE prompt ALTER COLUMN capability SET DEFAULT 'chat'::capability;
ALTER TABLE prompt ALTER COLUMN capability SET DEFAULT 'chat'::capability;
DROP TYPE capability_antigo;
"""

DOWN = """
ALTER TYPE capability RENAME TO capability_antigo;
CREATE TYPE capability AS ENUM ('chat', 'embed', 'rerank', 'ocr', 'pdf', 'matching');
ALTER TABLE provider ALTER COLUMN capabilities DROP DEFAULT;
ALTER TABLE prompt ALTER COLUMN capability DROP DEFAULT;
ALTER TABLE prompt ALTER COLUMN capability DROP DEFAULT;
ALTER TABLE provider_capability
    ALTER COLUMN capability TYPE capability
    USING (CASE capability::text WHEN 'extract' THEN 'pdf'
                                 ELSE capability::text END)::capability;
-- Sem subconsulta: o PG nao a aceita em USING. `extract` nao e substring de nenhum outro
-- rotulo, entao o replace sobre o literal do array e seguro.
ALTER TABLE provider
    ALTER COLUMN capabilities TYPE capability[]
    USING replace(capabilities::text, 'extract', 'pdf')::capability[];
ALTER TABLE prompt
    ALTER COLUMN capability TYPE capability
    USING (CASE capability::text WHEN 'extract' THEN 'pdf'
                                 ELSE capability::text END)::capability;
ALTER TABLE llm_call
    ALTER COLUMN capability TYPE capability
    USING (CASE capability::text WHEN 'extract' THEN 'pdf'
                                 ELSE capability::text END)::capability;
ALTER TABLE provider ALTER COLUMN capabilities SET DEFAULT '{}'::capability[];
ALTER TABLE prompt ALTER COLUMN capability SET DEFAULT 'chat'::capability;
ALTER TABLE prompt ALTER COLUMN capability SET DEFAULT 'chat'::capability;
DROP TYPE capability_antigo;

CREATE TYPE extraction_strategy AS ENUM ('window', 'full', 'vision');

ALTER TABLE item_enriquecido
    ADD COLUMN estrategia extraction_strategy NOT NULL DEFAULT 'window';

ALTER TABLE documento_extracao
    DROP CONSTRAINT IF EXISTS documento_extracao_numero_controle_pncp_key,
    DROP COLUMN IF EXISTS tabela_texto,
    ADD COLUMN estrategia extraction_strategy NOT NULL DEFAULT 'window',
    ADD COLUMN itens_json jsonb,
    ADD COLUMN n_paginas_ocr integer;

ALTER TABLE documento_extracao
    ADD CONSTRAINT documento_extracao_numero_controle_pncp_estrategia_key
        UNIQUE (numero_controle_pncp, estrategia);

CREATE TABLE documento_pagina (
    numero_controle_pncp text NOT NULL
        REFERENCES documento(numero_controle_pncp) ON DELETE CASCADE,
    arquivo text NOT NULL,
    pagina integer NOT NULL,
    fonte text NOT NULL,
    texto text NOT NULL,
    n_chars integer GENERATED ALWAYS AS (length(texto)) STORED,
    PRIMARY KEY (numero_controle_pncp, arquivo, pagina)
);
"""


def upgrade() -> None:
    op.execute(UP)
    op.execute(UP_SEM_OCR)


def downgrade() -> None:
    op.execute(DOWN)
