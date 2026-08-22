"""chave de API do provedor sai do .env e vai para o banco, cifrada (Fase 14, ADR-022)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-22

`provedor.api_key_ref` guardava o NOME de uma variável de ambiente; o valor morava no `.env`.
Isso mantinha o segredo fora do `pg_dump`, mas ao custo de a tela de provedores nunca poder
cadastrar um provedor sozinha — sempre havia um arquivo para editar e um servidor para
reiniciar. A ADR-022 troca a fronteira: a chave vem para o banco cifrada em AES-GCM, e o que
fica fora é só a chave-mestra (`APP_SECRET_KEY`), no ambiente do processo.

Três colunas novas e nenhuma perda de dado nesta revisão: `api_key_ref` é **mantida** aqui de
propósito. A migração de conteúdo (ler a env var apontada, cifrar, gravar) é o bloco 4 da
Fase 14, junto com o seed dos provedores — separar as duas coisas deixa o `downgrade` desta
revisão trivial e permite rodar os blocos 2 e 3 com o `.env` ainda no lugar.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provedor", sa.Column("api_key_cifrada", sa.LargeBinary(), nullable=True))
    # Só para a tela exibir `sk-or-…7b9d`: nunca o suficiente para reconstruir a chave.
    op.add_column("provedor", sa.Column("api_key_last4", sa.Text(), nullable=True))
    # Qual chave-mestra cifrou a linha. Existe desde o primeiro dia porque rotacionar a
    # `APP_SECRET_KEY` depois, sem saber o que já foi re-cifrado, seria adivinhação.
    op.add_column("provedor", sa.Column("api_key_key_id", sa.Text(), nullable=True))
    op.add_column("provedor", sa.Column(
        "api_key_atualizada_em", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("provedor", "api_key_atualizada_em")
    op.drop_column("provedor", "api_key_key_id")
    op.drop_column("provedor", "api_key_last4")
    op.drop_column("provedor", "api_key_cifrada")
