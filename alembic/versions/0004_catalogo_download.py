"""catalogo_download — checkpoint de página da etapa 0a sem tocar em disco (Fase 10, bloco B)

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19

Espelho no banco de `data/checkpoints/0a_parts_<tipo>/`, a pasta de parquet-partes que hoje
torna o download do catálogo resumível (uma parte por página; página já gravada é pulada).

Poderia não existir: `catalogo_raw` é upsert por `(tipo, codigo)`, então rebaixar uma página
já baixada é inofensivo. O que não é inofensivo é rebaixar as **700 páginas** por causa de uma
queda na última — que é exatamente o cenário que a pasta de partes foi criada para evitar
(ver a docstring de `coletar_para_partes`). O checkpoint por página preserva essa propriedade.

`prefixo` reproduz o nome do arquivo-parte: `full` para o download completo, `g<codigo>` para
o modo `--so-grupos-seguranca`, que pagina cada grupo separadamente. Sem ele, as páginas de
dois grupos diferentes colidiriam na PK.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE catalogo_download (
    tipo             tipo_catalogo NOT NULL,
    prefixo          text NOT NULL,   -- 'full' | 'g<codigoGrupo>'
    pagina           integer NOT NULL,
    n_linhas         integer NOT NULL DEFAULT 0,
    baixado_em       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, prefixo, pagina)
);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalogo_download;")
