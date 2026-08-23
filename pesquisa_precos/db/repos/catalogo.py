"""
Repositório do catálogo CATMAT/CATSER (`catalogo_item`, `catalogo_snapshot`).

`(tipo, codigo)` é a PK: o código só é único DENTRO do tipo. Todo lugar que hoje trata o
código como identificador global (os CSVs de par e de grupo trazem só `codigo`) precisa
resolver o tipo por aqui — e `tipo_do_codigo()` existe justamente para isso, incluindo a
detecção de colisão que docs/05_MIGRACAO.md §m12 manda validar antes de assumir.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy
from pesquisa_precos.db.models import CatalogoItem

COLUNAS = ("tipo", "codigo", "codigo_pdm", "nome_pdm", "descricao",
           "codigo_grupo", "nome_grupo", "nome_classe", "categoria", "ativo")


def gravar_itens(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert em massa. `linhas` na ordem de `COLUNAS`.

    `DO UPDATE` (e não `DO NOTHING`) porque a etapa 0a rebaixa o catálogo inteiro a cada
    execução: descrição e classe mudam no CATMAT, e manter a versão antiga faria o export
    divergir da fonte oficial sem nenhum sinal.
    """
    return copy.copiar(
        conn, "catalogo_item", COLUNAS, linhas,
        conflito=("tipo", "codigo"),
        atualizar=("codigo_pdm", "nome_pdm", "descricao", "codigo_grupo",
                   "nome_grupo", "nome_classe", "categoria", "ativo"),
    )


def marcar_inativos(sessao: Session, codigos: Sequence[tuple[str, str]]) -> int:
    """Códigos com status 'removido' no delta da 0a viram `ativo = false`.

    Desativa, nunca apaga: o item removido do catálogo continua sendo a origem de linhas de
    export já entregues, e apagá-lo quebraria a rastreabilidade (requisito nº 4 do projeto).
    """
    if not codigos:
        return 0
    n = 0
    for tipo, codigo in codigos:
        n += sessao.execute(
            text("UPDATE catalogo_item SET ativo = false, atualizado_em = now() "
                 "WHERE tipo = CAST(:t AS tipo_catalogo) AND codigo = :c AND ativo"),
            {"t": tipo, "c": codigo},
        ).rowcount
    return n


def codigos_removidos(sessao: Session) -> set[str]:
    """Códigos inativos — o que a etapa 8 poda do export final."""
    return set(sessao.scalars(
        select(CatalogoItem.codigo).where(CatalogoItem.ativo.is_(False))).all())


def tipo_do_codigo(sessao: Session) -> tuple[dict[str, str], list[str]]:
    """`codigo → tipo` e a lista de códigos AMBÍGUOS (presentes nos dois tipos).

    Os CSVs herdados de par/grupo guardam só o código. Resolver o tipo por join é seguro
    enquanto o código for único no catálogo filtrado — premissa que docs/05_MIGRACAO.md §m12
    manda validar, não assumir. Quem chama decide o que fazer com a lista de colisões; o
    m12 aborta.
    """
    linhas = sessao.execute(text(
        "SELECT codigo, array_agg(DISTINCT tipo::text) AS tipos "
        "FROM catalogo_item GROUP BY codigo")).all()
    mapa: dict[str, str] = {}
    ambiguos: list[str] = []
    for codigo, tipos in linhas:
        if len(tipos) > 1:
            ambiguos.append(codigo)
        mapa[codigo] = tipos[0]
    return mapa, ambiguos


def texto_por_codigo(sessao: Session) -> dict[str, dict]:
    """`codigo → {tipo, nome_pdm, descricao, nome_classe}` — o que a etapa 8 escreve no XLSX.

    Substitui `core.text.texto_catalogo()` / `e8.carregar_catalogo()` quando a fonte é o
    banco. Devolve o catálogo INTEIRO (2.212 linhas): não vale a pena paginar.
    """
    linhas = sessao.execute(text(
        "SELECT codigo, tipo::text, nome_pdm, descricao, nome_classe FROM catalogo_item")).all()
    return {
        codigo: {"tipo": tipo, "nome_pdm": nome_pdm or "",
                 "descricao": descricao or "", "nome_classe": nome_classe or ""}
        for codigo, tipo, nome_pdm, descricao, nome_classe in linhas
    }


def contar(sessao: Session) -> int:
    return sessao.execute(text("SELECT count(*) FROM catalogo_item")).scalar_one()
