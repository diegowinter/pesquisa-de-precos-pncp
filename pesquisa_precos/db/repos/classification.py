"""
Repositório de classificação (`texto_classificacao`, `item_categoria`).

A tabela cara é chaveada por TEXTO, não por item: 320 mil linhas em vez de 1,6 milhão. É o
dedup de ~5x da etapa 3, que aqui deixa de ser intra-execução e vira permanente (ADR-007) —
um texto classificado hoje não volta a custar nada nunca mais.

`item_categoria` é derivada e barata: `recomputar_item_categoria()` a reconstrói inteira por
SQL puro. Se um dia o prompt mudar, apagar `texto_classificacao` da versão antiga e
reclassificar é uma operação delimitada, sem tocar em `item`.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS = ("texto_hash", "descricao", "unidade", "categorias", "confianca",
           "prompt_versao_id", "modelo", "provedor", "run_id")

# O LLM devolve a confiança como PALAVRA ('alta'/'media'/'baixa'/'erro'); a coluna é `real`.
# A escala é ORDINAL e declarada — não é probabilidade, e tratá-la como tal inventaria uma
# precisão que o dado nunca teve. 'erro' vira NULL: aquela linha não é uma classificação, é a
# marca de uma chamada que falhou.
#
# Fonte ÚNICA da escala: a migração `m08` importa daqui. Duas tabelas de conversão divergindo
# fariam o mesmo texto ter confiança diferente conforme tivesse vindo do CSV ou da etapa.
CONFIANCA_ORDINAL: dict[str, float | None] = {
    "alta": 1.0, "media": 0.6, "média": 0.6, "baixa": 0.3, "erro": None,
}


def confianca_para_real(valor: str | float | None) -> float | None:
    """Palavra do LLM → `real`. Número já numérico passa direto; desconhecido vira NULL."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, int | float):
        return float(valor)
    return CONFIANCA_ORDINAL.get(str(valor).strip().lower())


def gravar(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert por `texto_hash` (ordem de `COLUNAS`). `categorias` é `list[str]` → `text[]`.

    `DO NOTHING`: reclassificar um texto já classificado é exatamente o gasto que esta tabela
    existe para evitar. Trocar de prompt/modelo é uma operação explícita (apagar as linhas da
    versão antiga), nunca um efeito colateral de rodar a etapa de novo.
    """
    return copy.copiar(conn, "texto_classificacao", COLUNAS, linhas,
                        conflito=("texto_hash",))


def hashes_ja_classificados(sessao: Session) -> set[str]:
    """Todos os `texto_hash` já pagos — o filtro de "o que ainda falta" da etapa 3."""
    return set(sessao.scalars(text("SELECT texto_hash FROM texto_classificacao")).all())


SQL_TEXTOS_PENDENTES = """
    SELECT DISTINCT ON (i.texto_hash)
           i.texto_hash, i.descricao_api, i.unidade,
           count(*) OVER (PARTITION BY i.texto_hash) AS n_itens
      FROM item i
     WHERE NOT EXISTS (SELECT 1 FROM texto_classificacao tc
                        WHERE tc.texto_hash = i.texto_hash)
     ORDER BY i.texto_hash, n_itens DESC
"""


def textos_pendentes(sessao: Session, limite: int | None = None) -> list[dict]:
    """Textos ÚNICOS ainda não classificados, do mais repetido para o menos.

    É o dedup da etapa 3 virando consulta: no CSV era preciso carregar 1,6 milhão de linhas
    em memória e agrupar por `(descricao, unidade)`; aqui o `texto_hash` já foi calculado na
    ingestão (etapa 2) e o agrupamento é do banco.

    A ordem por `n_itens` não é cosmética: com `--limite`, classificar primeiro os textos que
    se repetem mais é o que dá mais cobertura de itens por chamada paga.
    """
    sql = SQL_TEXTOS_PENDENTES + ("\n LIMIT :limite" if limite else "")
    linhas = sessao.execute(text(sql), {"limite": limite} if limite else {}).all()
    return [{"texto_hash": h, "descricao": d or "", "unidade": u, "n_itens": n}
            for h, d, u, n in linhas]


def contar_pendentes(sessao: Session) -> tuple[int, int]:
    """(textos únicos pendentes, itens que eles cobrem) — o que a estimativa precisa."""
    linha = sessao.execute(text("""
        SELECT count(DISTINCT i.texto_hash), count(*)
          FROM item i
         WHERE NOT EXISTS (SELECT 1 FROM texto_classificacao tc
                            WHERE tc.texto_hash = i.texto_hash)
    """)).one()
    return int(linha[0]), int(linha[1])


def recomputar_item_categoria(sessao: Session) -> int:
    """Reconstrói `item_categoria` a partir do join item × texto_classificacao.

    É o SQL de docs/02_SCHEMA.md §5, literal. Aditivo (`ON CONFLICT DO NOTHING`): não remove
    categoria que tenha deixado de valer. Para um recorte limpo, chamar
    `limpar_item_categoria()` antes — deixar isso explícito evita que uma reclassificação
    parcial apague o multi-label de 400 mil itens sem ninguém pedir.
    """
    return sessao.execute(text("""
        INSERT INTO item_categoria (item_key, categoria)
        SELECT i.item_key, unnest(tc.categorias)
        FROM item i JOIN texto_classificacao tc USING (texto_hash)
        ON CONFLICT DO NOTHING
    """)).rowcount


def limpar_item_categoria(sessao: Session) -> int:
    return sessao.execute(text("DELETE FROM item_categoria")).rowcount


def categorias_por_item(sessao: Session, apenas_sobreviventes: bool = True) -> dict[str, list[str]]:
    """`item_key → [categorias]`. Usado pelo pareamento e pela conferência do corte."""
    filtro = "JOIN item i USING (item_key) WHERE i.sobrevivente" if apenas_sobreviventes else ""
    linhas = sessao.execute(text(
        f"SELECT item_key, array_agg(categoria ORDER BY categoria) "
        f"FROM item_categoria {filtro} GROUP BY item_key")).all()
    return {k: list(v) for k, v in linhas}


def contar(sessao: Session) -> dict[str, int]:
    return {
        "texto_classificacao": sessao.execute(
            text("SELECT count(*) FROM texto_classificacao")).scalar_one(),
        "item_categoria": sessao.execute(
            text("SELECT count(*) FROM item_categoria")).scalar_one(),
    }
