"""grupo_permitido — grupos de segurança pública saem do código (Fase 10, bloco B)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-20

Fecha a outra metade do ADR-017. A 0003 tirou do código a allow-list de PDMs
(`PDMS_MATERIAIS`/`CODIGOS_SERVICOS`); ficaram `GRUPOS_MATERIAIS`/`GRUPOS_SERVICOS`, em
`core/catalogo/local.py`.

Tabela SEPARADA de `pdm_permitido`, e não uma coluna `especie` na mesma: as duas curadorias
respondem a perguntas diferentes e são usadas em momentos diferentes do fluxo.

    pdm_permitido    define o ESCOPO — quais itens do catálogo entram na pesquisa.
                     Aplicado na derivação de `catalogo_item`, sempre.
    grupo_permitido  define o RECORTE DO DOWNLOAD — quais `codigoGrupo` a 0a pagina quando
                     roda com `--so-grupos-seguranca`. Não filtra escopo: sem a flag, a etapa
                     baixa o catálogo inteiro e esta tabela não é consultada.

Juntá-las numa só faria a tela de curadoria misturar "o que eu pesquiso" com "o que eu baixo
para poder escolher" — e o segundo é otimização de tempo de download, não decisão de negócio.

O seed reproduz as duas constantes como estavam em 2026-08-20. `codigo` é `text` (e não
`integer`) para casar com `catalogo_raw.codigo_grupo`, que também é `text`: o catálogo mistura
int e str no mesmo campo entre os endpoints de material e serviço.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE grupo_permitido (
    tipo             tipo_catalogo NOT NULL,
    codigo           text NOT NULL,   -- codigoGrupo do CATMAT/CATSER
    nome             text,
    observacao       text,
    ativo            boolean NOT NULL DEFAULT true,
    criado_por       text,
    criado_em        timestamptz NOT NULL DEFAULT now(),
    atualizado_em    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, codigo)
);
CREATE INDEX ix_grupo_permitido_ativo ON grupo_permitido (tipo, codigo) WHERE ativo;
"""

# Reprodução literal de core/catalogo/local.py em 2026-08-20.
GRUPOS_MATERIAIS = [10, 12, 13, 15, 23, 25, 42, 58, 62, 67, 68, 70, 74, 84]
GRUPOS_SERVICOS = [841, 851, 852, 929, 931, 965]

# O único grupo com motivo registrado no código — o comentário vira dado, como na 0003.
OBSERVACOES = {
    (25, "material"): "entrou junto com o PDM 1665 (ACESSÓRIO CARRO BLINDADO), classe 2590",
}


def _seed() -> str:
    linhas = []
    for codigo in GRUPOS_MATERIAIS:
        obs = OBSERVACOES.get((codigo, "material"))
        linhas.append(f"('material', '{codigo}', " + (f"'{obs}'" if obs else "NULL") + ")")
    for codigo in GRUPOS_SERVICOS:
        linhas.append(f"('servico', '{codigo}', NULL)")
    valores = ",\n    ".join(linhas)
    return (
        "INSERT INTO grupo_permitido (tipo, codigo, observacao, criado_por) "
        "SELECT v.tipo::tipo_catalogo, v.codigo, v.observacao, 'seed:0006' "
        f"FROM (VALUES\n    {valores}\n) AS v(tipo, codigo, observacao) "
        "ON CONFLICT (tipo, codigo) DO NOTHING;"
    )


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_seed())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS grupo_permitido;")
