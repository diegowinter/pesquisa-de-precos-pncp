"""preço por chamada vira coluna do provedor (Fase 14 bloco 4, ADR-022)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-22

`CUSTO_USD_CHAMADA_PASS1`/`PASS2` eram as duas últimas chaves de operação no `.env` sem
destino no banco. Elas não cabem em `custo_in_por_mtok`/`custo_out_por_mtok`: converter preço
por chamada em preço por Mtok exigiria inventar um tamanho médio de prompt e de resposta, e o
`estimar()` das etapas prefere responder "não estimado" a inventar (comentário original em
`config/settings.py`).

Então ganham coluna própria, por provedor — que é o recorte certo: o preço por chamada é uma
característica do par (provedor, modelo), não da instalação. A medição real por tokens
(`llm_chamada`) segue sendo a fonte para custo consumado; esta coluna é só para a ESTIMATIVA
prévia, e quem a conhece é o operador.

`NULL` é significativo: "não informado" → `Estimativa.custo_usd = None`. Não confundir com
`0.0`, que é o caso legítimo do provedor local (LM Studio na GPU caseira, que não custa nada).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provedor", sa.Column("custo_usd_chamada", sa.Numeric(10, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("provedor", "custo_usd_chamada")
