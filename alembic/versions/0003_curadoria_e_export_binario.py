"""catalogo_raw + pdm_permitido + export.conteudo (Fase 10, bloco A)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-19

Três mudanças, todas exigidas pela execução em servidor:

1. `catalogo_raw` — o CATMAT/CATSER COMPLETO passa a viver no banco (ADR-017). Antes ele só
   existia nos parquet de `data/`, o que tornava impossível escolher PDM pela interface: a
   tela precisa listar o que existe, não só o que já foi curado.

   O formato é o NORMALIZADO (as 8 colunas do antigo `0a_catalogo_filtrado.csv`), não o bruto
   da API — material e serviço têm nomes de campo diferentes (`descricaoItem`/`nomePdm` vs.
   `nomeServico`/`nomeSubclasse`) e unificá-los na ingestão é o que permite a derivação do
   item 3 ser SQL puro. "raw" aqui quer dizer COMPLETO (sem allow-list), não "cru".

2. `pdm_permitido` — a allow-list sai de `core/catalogo/local.py` e vira dado (ADR-017).
   Semântica do `codigo` por tipo, herdada de `filtrar_curado()`:
       material → codigoPdm      servico → codigoServico
   O seed abaixo reproduz `PDMS_MATERIAIS` e `CODIGOS_SERVICOS` exatamente como estavam no
   código em 2026-08-19, comentários inclusive (viraram `observacao`). Sem o seed, a primeira
   execução da 0a após esta migration devolveria catálogo vazio.

3. `export.conteudo` — o XLSX passa a viver no banco (ADR-018 §2). `arquivo` perde o NOT NULL
   em vez de ser removida: as linhas de export já geradas apontam para caminhos que existiram,
   e apagá-las reescreveria histórico.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE catalogo_raw (
    tipo             tipo_catalogo NOT NULL,
    codigo           text NOT NULL,
    codigo_pdm       text,
    nome_pdm         text,
    descricao        text NOT NULL,
    codigo_grupo     text,
    nome_grupo       text,
    nome_classe      text,
    baixado_em       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo)
);
-- A derivação `catalogo_raw ∩ pdm_permitido` casa por codigo_pdm (material) e por codigo
-- (servico). Sem este índice o join varre o catálogo inteiro a cada recuradoria.
CREATE INDEX ix_catalogo_raw_codigo_pdm ON catalogo_raw (codigo_pdm);
CREATE INDEX ix_catalogo_raw_grupo      ON catalogo_raw (codigo_grupo);

CREATE TABLE pdm_permitido (
    tipo             tipo_catalogo NOT NULL,
    codigo           text NOT NULL,   -- material: codigoPdm · servico: codigoServico
    nome             text,
    observacao       text,
    ativo            boolean NOT NULL DEFAULT true,
    criado_por       text,
    criado_em        timestamptz NOT NULL DEFAULT now(),
    atualizado_em    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo)
);
CREATE INDEX ix_pdm_permitido_ativo ON pdm_permitido (tipo, codigo) WHERE ativo;

ALTER TABLE export ADD COLUMN conteudo bytea;
ALTER TABLE export ADD COLUMN nome_arquivo text;
ALTER TABLE export ALTER COLUMN arquivo DROP NOT NULL;
"""

# Reprodução literal de core/catalogo/local.py em 2026-08-19. Os comentários que existiam lá
# viraram `observacao` — é curadoria com motivo registrado, e o motivo é o que se perde
# primeiro quando uma lista sai do código.
PDMS_MATERIAIS = [
    2995, 2994, 16657, 1423, 17996, 1425, 18582, 8079, 11255, 15345,
    1243, 13849, 14570, 15692, 1024, 1431, 13401, 4452, 4453, 6661,
    8435, 19246, 224, 226, 238, 10293, 216, 13174, 17653, 3142,
    14419, 16796, 14411, 14329, 648, 10218, 14415, 14408, 653, 16741,
]
PDMS_COM_MOTIVO = {
    805: "VEÍCULO TRANSPORTE PRESO — veículo de segurança que faltava na classe 2320",
    4595: "CARRO BLINDADO — veículo de segurança que faltava na classe 2320",
    1665: "ACESSÓRIO CARRO BLINDADO (grupo 25 / classe 2590)",
    16793: "VEÍCULO ESPECIAL — veículo de segurança que faltava na classe 2320",
    2396: "AMBULÂNCIA — veículo de segurança que faltava na classe 2320",
}
CODIGOS_SERVICOS = [
    10014, 18384, 23809, 23817, 23833, 23841, 23825, 23850, 23868, 19631, 22870,
]

# Ficaram DELIBERADAMENTE fora da allow-list. Entram como `ativo = false` em vez de não
# entrarem: sem isso, a próxima pessoa a olhar a tela de curadoria não tem como saber que a
# ausência foi decisão, e não esquecimento — e o argumento se perde de novo.
EXCLUIDOS = {
    685: "VEÍCULO AUTOMOTIVO - PICAPE — PDM legado sem item ativo; o vivo p/ picape é o 14419",
    4613: "CARRO FORTE — PDM legado sem item ativo",
}


def _seed() -> str:
    linhas = []
    for codigo in PDMS_MATERIAIS:
        linhas.append(f"('material', '{codigo}', NULL, true)")
    for codigo, motivo in PDMS_COM_MOTIVO.items():
        linhas.append(f"('material', '{codigo}', '{motivo}', true)")
    for codigo, motivo in EXCLUIDOS.items():
        linhas.append(f"('material', '{codigo}', '{motivo}', false)")
    for codigo in CODIGOS_SERVICOS:
        linhas.append(f"('servico', '{codigo}', NULL, true)")
    valores = ",\n    ".join(linhas)
    return (
        "INSERT INTO pdm_permitido (tipo, codigo, observacao, ativo, criado_por) "
        "SELECT v.tipo::tipo_catalogo, v.codigo, v.observacao, v.ativo, 'seed:0003' "
        f"FROM (VALUES\n    {valores}\n) AS v(tipo, codigo, observacao, ativo) "
        "ON CONFLICT (tipo, codigo) DO NOTHING;"
    )


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_seed())


def downgrade() -> None:
    op.execute(
        "ALTER TABLE export DROP COLUMN IF EXISTS conteudo;"
        "ALTER TABLE export DROP COLUMN IF EXISTS nome_arquivo;"
        "DROP TABLE IF EXISTS pdm_permitido;"
        "DROP TABLE IF EXISTS catalogo_raw;"
    )
