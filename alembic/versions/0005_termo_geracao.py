"""termo_geracao — cache por item da geração de termos da etapa 1 (Fase 10, bloco B)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-20

Equivalente no banco de `data/checkpoints/1_termos_item.csv`: a saída BRUTA do LLM para cada
item do catálogo (os termos como o modelo os devolveu, mais a categoria que ele sugeriu).

Por que não derivar isso de `termo` + `termo_codigo`, que já existem — a pergunta certa a
fazer antes de criar tabela: porque as duas guardam o resultado JÁ AGREGADO e JÁ EXPANDIDO.
`expandir_termos()` aplica variações de grafia e duplica cada termo sem acento antes de
gravar; reconstruir o checkpoint a partir dali reexpandiria o que já foi expandido. Pior, o
`resolver_categorias()` usa o conjunto de termos CRU do item como chave de desempate da
categoria — com o conjunto expandido, itens que hoje caem na mesma cesta deixariam de cair, e
a categoria de alguns códigos mudaria em silêncio.

O mesmo raciocínio de `texto_classificacao` (ADR-007) se aplica: é o cache do que custou
chamada de LLM, e sobreviver entre runs é o que impede pagar de novo pelo mesmo item.
`categoria_llm` é a sugestão do modelo, NÃO a categoria final — a final sai da cascata de
`resolver_categorias()` e vive em `catalogo_item.categoria`. Guardar as duas é o que permite
recomputar a cascata sem rechamar o LLM.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE termo_geracao (
    tipo             tipo_catalogo NOT NULL,
    codigo           text NOT NULL,
    termos           text[] NOT NULL DEFAULT '{}',
    categoria_llm    text,
    modelo           text,
    provedor         text,
    prompt_versao_id bigint REFERENCES prompt_versao(id),
    run_id           bigint REFERENCES run(id),
    criado_em        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo),
    FOREIGN KEY (tipo, codigo) REFERENCES catalogo_item(tipo, codigo) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS termo_geracao;")
