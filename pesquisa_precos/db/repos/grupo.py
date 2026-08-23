"""
Repositório do resultado (`grupo_item`, `faixa_preco`, `export`, `export_snapshot`).

`grupo_item` é append-only por `run_id` (ADR-015): cada execução da step 7 grava o seu
ranking, e nada apaga o da execução anterior. É o que sustenta o diff entre runs da Fase 9 —
e é, segundo o próprio ADR, a decisão de modelagem mais cara de mudar depois.

`export_snapshot` é o baseline do `--novos`. ARMADILHA conhecida: a primeira execução sem
snapshot marca TUDO como novo. A correção é semear a partir do último export oficial (m16),
não tratar como bug.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS_GRUPO = ("tipo", "codigo", "item_key", "par_key", "posicao",
                 "preco_unitario", "flag_preco", "motivo_flag", "run_id")


def gravar(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Grava o ranking de um run (ordem de `COLUNAS_GRUPO`)."""
    return copy.copiar(
        conn, "grupo_item", COLUNAS_GRUPO, linhas,
        conflito=("run_id", "tipo", "codigo", "item_key"),
        atualizar=("par_key", "posicao", "preco_unitario", "flag_preco", "motivo_flag"),
    )


def limpar_run(sessao: Session, run_id: int) -> int:
    """Apaga o ranking DESTE run — usado quando a step 7 é reexecutada dentro do mesmo run.

    Não é violação do append-only: o que é imutável é o resultado de runs anteriores. Refazer
    a 7 no run corrente sem limpar deixaria posições órfãs de um corte que não existe mais.
    """
    return sessao.execute(
        text("DELETE FROM grupo_item WHERE run_id = :r"), {"r": run_id}).rowcount


def linhas_do_run(sessao: Session, run_id: int) -> list[dict]:
    """Ranking do run, já com tudo que a step 8 escreve no XLSX.

    Um SELECT só, com os mesmos joins da consulta de auditoria de docs/02_SCHEMA.md §12 —
    o que na prática é o teste de fogo do schema rodando em produção.
    """
    linhas = sessao.execute(text("""
        SELECT g.tipo::text AS tipo, g.codigo, g.item_key, g.par_key, g.posicao,
               g.preco_unitario, g.flag_preco,
               i.numero_item, i.unidade, i.quantidade, i.preco_estimado,
               i.fornecedor, i.data_resultado,
               d.numero_controle_pncp, d.tipo_doc::text AS tipo_doc, d.orgao, d.orgao_cnpj,
               d.uf, d.data, d.ano, d.data_fim_vigencia, d.data_assinatura, d.url_pncp,
               COALESCE(NULLIF(e.descricao_final, ''), i.descricao_api) AS descricao_final,
               c.nome_pdm, c.description AS descricao_catalogo, c.nome_classe, c.active
          FROM grupo_item g
          JOIN item i      ON i.item_key = g.item_key
          JOIN documento d ON d.numero_controle_pncp = i.numero_controle_pncp
          JOIN catalogo_item c ON c.tipo = g.tipo AND c.codigo = g.codigo
          LEFT JOIN item_enriquecido e ON e.item_key = g.item_key
         WHERE g.run_id = :r
         ORDER BY g.codigo, g.posicao
    """), {"r": run_id}).mappings().all()
    return [dict(r) for r in linhas]


def ultimo_run_com_grupos(sessao: Session) -> int | None:
    """Run mais recente que produziu ranking — o default da step 8 quando não se passa um."""
    return sessao.execute(
        text("SELECT max(run_id) FROM grupo_item")).scalar()


# ── Faixas de preço ─────────────────────────────────────────────────────────────────

def gravar_faixas(sessao: Session,
                  faixas: Sequence[tuple[str, Decimal | None, Decimal | None]]) -> int:
    n = 0
    for categoria, minimo, maximo in faixas:
        n += sessao.execute(
            text("INSERT INTO faixa_preco (categoria, preco_min, preco_max) "
                 "VALUES (:c, :mi, :ma) "
                 "ON CONFLICT (categoria) DO UPDATE "
                 "SET preco_min = EXCLUDED.preco_min, preco_max = EXCLUDED.preco_max"),
            {"c": categoria, "mi": minimo, "ma": maximo},
        ).rowcount
    return n


def faixas(sessao: Session) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    linhas = sessao.execute(
        text("SELECT categoria, preco_min, preco_max FROM faixa_preco")).all()
    return {c: (mi, ma) for c, mi, ma in linhas}


# ── Export e snapshot do `--novos` ──────────────────────────────────────────────────

def registrar_export(sessao: Session, run_id: int, tipo: str, arquivo: str | None,
                     n_linhas: int, n_codigos: int, hash_arquivo: str | None = None,
                     *, conteudo: bytes | None = None,
                     nome_arquivo: str | None = None) -> int:
    """Uma linha por export gerado. Com `conteudo`, o XLSX vive NO BANCO (ADR-018 §2).

    `arquivo` (caminho relativo) continua aceito para o caminho `--fonte csv`, onde o arquivo
    de fato existe em disco. No caminho banco ele é NULL e quem serve o download são os bytes.
    """
    return sessao.execute(
        text("INSERT INTO export (run_id, tipo, arquivo, nome_arquivo, conteudo, "
             "                    n_linhas, n_codigos, hash_arquivo) "
             "VALUES (:r, :t, :a, :na, :co, :nl, :nc, :h) RETURNING id"),
        {"r": run_id, "t": tipo, "a": arquivo, "na": nome_arquivo, "co": conteudo,
         "nl": n_linhas, "nc": n_codigos, "h": hash_arquivo},
    ).scalar_one()


def conteudo_export(sessao: Session, export_id: int) -> tuple[str, bytes] | None:
    """(nome_arquivo, bytes) — o que a web serve no download quando o export vive no banco."""
    linha = sessao.execute(
        text("SELECT coalesce(nome_arquivo, 'export.xlsx'), conteudo FROM export "
             " WHERE id = :i AND conteudo IS NOT NULL"), {"i": export_id}).first()
    return (linha[0], bytes(linha[1])) if linha else None


def snapshot(sessao: Session) -> set[tuple[str, str, str, int]]:
    """Chaves do último export `--novos`. Vazio na 1ª vez → tudo é novo (ver docstring)."""
    return {
        (t, c, nc, ni)
        for t, c, nc, ni in sessao.execute(text(
            "SELECT tipo::text, codigo, numero_controle_pncp, numero_item "
            "FROM export_snapshot")).all()
    }


def avancar_snapshot(conn: psycopg.Connection, chaves: Sequence[tuple], export_id: int | None,
                     *, substituir: bool = True) -> int:
    """Grava as chaves do export atual como novo baseline.

    `substituir=True` faz `DELETE` antes: o snapshot é um retrato do último export, não um
    acumulado. Sem o delete, uma linha que saiu do export (código removido do catálogo)
    continuaria marcada como "já entregue" para sempre.
    """
    with conn.cursor() as cur:
        if substituir:
            cur.execute("DELETE FROM export_snapshot")
    linhas = [(t, c, nc, ni, export_id) for t, c, nc, ni in chaves]
    return copy.copiar(
        conn, "export_snapshot",
        ("tipo", "codigo", "numero_controle_pncp", "numero_item", "export_id"),
        linhas, conflito=("tipo", "codigo", "numero_controle_pncp", "numero_item"),
    )


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "grupo_item": "SELECT count(*) FROM grupo_item",
        "faixa_preco": "SELECT count(*) FROM faixa_preco",
        "export_snapshot": "SELECT count(*) FROM export_snapshot",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
