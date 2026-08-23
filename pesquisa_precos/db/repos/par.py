"""
Repositório de pareamento (`par`, `rotulo`, `embedding_cache`).

Uma tabela `par` para as etapas 6a, 6b e 6c (ADR-013). Cada etapa escreve só as SUAS colunas —
daí as três funções de gravação separadas, todas com `ON CONFLICT DO UPDATE` restrito. Uma
função genérica que sobrescrevesse a linha inteira apagaria o score do reranker toda vez que a
6a recomputasse os candidatos.

`decisao_final` é derivada: `confirmado` = (`decisao='aceito'`) OU (`veredito='sim'`).
`recomputar_decisao_final()` a reconstrói; é chamada ao fim da 6c.
"""

from collections.abc import Sequence
from typing import Any

import numpy as np
import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS_6A = ("par_key", "tipo", "codigo", "item_key", "categoria",
              "score_bm25", "score_cosseno", "sobreviveu", "run_id")

COLUNAS_ROTULO = ("par_key", "texto_catalogo", "texto_item", "score_rerank",
                  "decisao_final", "origem", "modelo", "run_id")


def gravar_candidatos(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Etapa 6a (ordem de `COLUNAS_6A`). Só as colunas da 6a são atualizadas."""
    return copy.copiar(
        conn, "par", COLUNAS_6A, linhas, conflito=("par_key",),
        atualizar=("score_bm25", "score_cosseno", "sobreviveu", "categoria", "run_id"),
    )


def gravar_rerank(sessao: Session,
                  linhas: Sequence[tuple[str, float | None, str | None]]) -> int:
    """Etapa 6b: (par_key, score_rerank, decisao) em lote, via `unnest`.

    Não usa `COPY` porque é UPDATE de linha existente, não inserção — e são 250 mil linhas,
    volume que o `unnest` com três arrays paralelos resolve numa consulta só.
    """
    if not linhas:
        return 0
    return sessao.execute(
        text("UPDATE par p "
             "   SET score_rerank = r.score, "
             "       decisao = CAST(NULLIF(r.decisao, '') AS decisao_rerank), "
             "       atualizado_em = now() "
             "  FROM unnest(CAST(:pks AS text[]), CAST(:scs AS real[]), "
             "              CAST(:dcs AS text[])) AS r(pk, score, decisao) "
             " WHERE p.par_key = r.pk"),
        {"pks": [p for p, _, _ in linhas],
         "scs": [s for _, s, _ in linhas],
         "dcs": [d or "" for _, _, d in linhas]},
    ).rowcount


def gravar_veredito(sessao: Session,
                    linhas: Sequence[tuple[str, str, str | None, str | None]]) -> int:
    """Etapa 6c: (par_key, veredito, justificativa, modelo) em lote."""
    if not linhas:
        return 0
    return sessao.execute(
        text("UPDATE par p "
             "   SET veredito = CAST(NULLIF(v.veredito, '') AS veredito_par), "
             "       justificativa = NULLIF(v.just, ''), "
             "       modelo_6c = NULLIF(v.modelo, ''), "
             "       atualizado_em = now() "
             "  FROM unnest(CAST(:pks AS text[]), CAST(:vds AS text[]), "
             "              CAST(:jus AS text[]), CAST(:mds AS text[])) "
             "       AS v(pk, veredito, just, modelo) "
             " WHERE p.par_key = v.pk"),
        {"pks": [p for p, _, _, _ in linhas],
         "vds": [v for _, v, _, _ in linhas],
         "jus": [j or "" for _, _, j, _ in linhas],
         "mds": [m or "" for _, _, _, m in linhas]},
    ).rowcount


def recomputar_decisao_final(sessao: Session) -> int:
    """`confirmado` = decisao='aceito' OU veredito='sim'; `rejeitado` quando há sinal
    contrário dos dois lados; `pendente` enquanto falta informação.

    Derivada e recomputável — nunca gravada à mão por uma etapa (docs/02_SCHEMA.md §7).
    """
    return sessao.execute(text("""
        UPDATE par SET decisao_final = CASE
            WHEN decisao = 'aceito' OR veredito = 'sim' THEN 'confirmado'
            WHEN veredito IN ('nao', 'indeterminado') THEN 'rejeitado'
            WHEN decisao = 'rejeitado' AND veredito IS NULL THEN 'rejeitado'
            ELSE 'pendente'
        END::decisao_final_par
        WHERE decisao_final IS DISTINCT FROM (CASE
            WHEN decisao = 'aceito' OR veredito = 'sim' THEN 'confirmado'
            WHEN veredito IN ('nao', 'indeterminado') THEN 'rejeitado'
            WHEN decisao = 'rejeitado' AND veredito IS NULL THEN 'rejeitado'
            ELSE 'pendente'
        END::decisao_final_par)
    """)).rowcount


def confirmados(sessao: Session) -> list[dict]:
    """Pares confirmados com os metadados do item — a entrada da etapa 7.

    Substitui `e7.confirmados()` + o merge com `4_itens_sobreviventes.csv`. Preço vem como
    `Decimal` (numeric no banco), nunca `float` — docs/08_CONVENCOES.md §5.8.
    """
    linhas = sessao.execute(text("""
        SELECT p.par_key, p.tipo::text AS tipo, p.codigo, p.item_key, p.categoria,
               d.tipo_doc::text AS tipo_doc, i.numero_controle_pncp, i.numero_item,
               i.unidade, i.quantidade, i.preco_unitario, i.preco_estimado,
               i.fornecedor, i.data_resultado,
               d.orgao, d.uf, d.data, d.ano, d.orgao_cnpj,
               d.data_fim_vigencia, d.data_assinatura,
               COALESCE(NULLIF(e.descricao_final, ''), i.descricao_api) AS descricao_final
          FROM par p
          JOIN item i      ON i.item_key = p.item_key
          JOIN documento d ON d.numero_controle_pncp = i.numero_controle_pncp
          LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
         WHERE p.decisao_final = 'confirmado'
    """)).mappings().all()
    return [dict(r) for r in linhas]


def gravar_rotulos(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Append-only, UNIQUE (par_key, origem). NUNCA truncar — é a base de calibração."""
    return copy.copiar(conn, "rotulo", COLUNAS_ROTULO, linhas,
                        conflito=("par_key", "origem"))


# ── Cache de embeddings ─────────────────────────────────────────────────────────────

def gravar_embeddings(conn: psycopg.Connection, provedor: str, modelo: str,
                      itens: Sequence[tuple[str, "np.ndarray"]]) -> int:
    """Grava vetores como float16 little-endian em `bytea`.

    A chave inclui provedor+modelo+dimensão (ADR-006 §1). Chavear só por texto misturaria
    espaços vetoriais em silêncio quando o provedor mudasse — o bug caro que a Fase 7 herda
    se isto estiver errado agora.

    float16 e não float32: os vetores são L2-normalizados e usados só para cosseno; meia
    precisão custa ~1e-3 de erro no score e corta o armazenamento pela metade (305 mil × 1024
    dimensões).
    """
    linhas = []
    for texto_hash, vetor in itens:
        v = np.asarray(vetor, dtype="<f2")
        linhas.append((texto_hash, provedor, modelo, int(v.size), v.tobytes()))
    return copy.copiar(
        conn, "embedding_cache",
        ("texto_hash", "provedor", "modelo", "dimensao", "vetor"), linhas,
        conflito=("texto_hash", "provedor", "modelo", "dimensao"),
    )


def ler_embeddings(sessao: Session, provedor: str, modelo: str,
                   dimensao: int) -> dict[str, "np.ndarray"]:
    """`texto_hash → vetor float32` para o espaço vetorial (provedor, modelo, dimensão)."""
    linhas = sessao.execute(
        text("SELECT texto_hash, vetor FROM embedding_cache "
             "WHERE provedor = :p AND modelo = :m AND dimensao = :d"),
        {"p": provedor, "m": modelo, "d": dimensao},
    ).all()
    return {h: np.frombuffer(v, dtype="<f2").astype(np.float32) for h, v in linhas}


def amostra_rotulos(sessao: Session, limite: int = 200) -> list[dict]:
    """Amostra de `rotulo` para a suite de regressão (Fase 9) — os mais recentes primeiro
    (`criado_em DESC`), até `limite` linhas. `rotulo` é append-only e cresce entre execuções
    (docs/02_SCHEMA.md §7); os mais recentes refletem melhor o estado atual do reranker."""
    linhas = sessao.execute(
        text("SELECT par_key, score_rerank, decisao_final FROM rotulo "
             "ORDER BY criado_em DESC LIMIT :n"), {"n": limite},
    ).mappings().all()
    return [dict(r) for r in linhas]


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "par": "SELECT count(*) FROM par",
        "par_confirmado": "SELECT count(*) FROM par WHERE decisao_final = 'confirmado'",
        "rotulo": "SELECT count(*) FROM rotulo",
        "embedding_cache": "SELECT count(*) FROM embedding_cache",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
