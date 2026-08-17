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

from pesquisa_precos.db import copia

COLUNAS = ("texto_hash", "descricao", "unidade", "categorias", "confianca",
           "prompt_versao_id", "modelo", "provedor", "run_id")


def gravar(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert por `texto_hash` (ordem de `COLUNAS`). `categorias` é `list[str]` → `text[]`.

    `DO NOTHING`: reclassificar um texto já classificado é exatamente o gasto que esta tabela
    existe para evitar. Trocar de prompt/modelo é uma operação explícita (apagar as linhas da
    versão antiga), nunca um efeito colateral de rodar a etapa de novo.
    """
    return copia.copiar(conn, "texto_classificacao", COLUNAS, linhas,
                        conflito=("texto_hash",))


def hashes_ja_classificados(sessao: Session) -> set[str]:
    """Todos os `texto_hash` já pagos — o filtro de "o que ainda falta" da etapa 3."""
    return set(sessao.scalars(text("SELECT texto_hash FROM texto_classificacao")).all())


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
