"""view `item_enriquecido_melhor` — UMA linha por item, a melhor

A ADR-024 trocou a PK de `item_enriquecido` de `(item_key)` para
`(item_key, numero_controle_pncp)`: o item pertence a COMPRA, e esta tabela registra em QUAL
documento dela ele foi encontrado. Um item passa a ter uma linha por documento em que foi
procurado — media de 3,47 no acervo, MAXIMO DE 47.

Os consumidores nao foram revisados junto, e todos faziam `LEFT JOIN item_enriquecido USING
(item_key)`. Resultado: cada item vira N linhas.

    etapa 6b : pontuou 1.707 pares como se fossem 4.770 (2,8x de GPU)
    etapa 6c : pagaria 2,8x de LLM pelo mesmo trabalho
    etapas 7/8: o mesmo item vira N referencias de preco — exatamente a duplicacao que a
                ADR-024 existe para eliminar, reintroduzida pela porta dos fundos

E havia um problema pior que o desperdicio: qual linha vencia era ARBITRARIO, decidido pela
ordem que o banco devolvesse. 852 itens tem uma linha com `descricao_final` vinda do PDF e
outra com a da API; para esses, o sorteio podia jogar fora a descricao rica.

A view resolve nos dois sentidos — deduplica E escolhe deterministicamente a melhor linha:
confirmado no PDF primeiro, depois a descricao mais longa, e o numero de controle como
criterio de desempate estavel (mesma entrada, mesma saida, sempre).

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UP = """
CREATE OR REPLACE VIEW item_enriquecido_melhor AS
SELECT DISTINCT ON (item_key) *
  FROM item_enriquecido
 ORDER BY item_key,
          (status::text LIKE 'pdf_ok%') DESC,   -- confirmado no documento vence
          (fonte_descricao = 'pdf') DESC,       -- descricao rica vence a da API
          length(coalesce(descricao_final, '')) DESC,
          numero_controle_pncp;                 -- desempate estavel
"""

DOWN = "DROP VIEW IF EXISTS item_enriquecido_melhor;"


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
