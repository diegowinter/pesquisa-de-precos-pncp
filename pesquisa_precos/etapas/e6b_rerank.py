"""
Etapa 6b — Reranker (cross-encoder) decide a maioria dos pares, custo zero de token.

Para cada par sobrevivente da 6a: score do cross-encoder bge-reranker sobre (texto_catalogo,
descricao_final do item). Decisão por threshold:
  score >= RERANK_T_ACEITA → aceito;  score <= RERANK_T_REJEITA → rejeitado;  entre → ambiguo.

Entrada: data/6a_pares_candidatos.csv (sobreviveu=true) + textos das saídas anteriores.
Saída: data/6b_pares_rerankeados.csv (par_key, score_rerank, decisao). Chave de resumo: par_key.
GPU: o reranker roda sozinho.

NÃO fazer: trocar o modelo do reranker sem recalibrar os thresholds — a base para isso é
data/6_rotulos_acumulados.csv (ver ferramentas/calibrar_thresholds.py).

Uso: python -m pesquisa_precos.etapas.e6b_rerank [--limite N] [--remoto]
"""

import sys
from typing import Literal

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "6b"
VERSAO_CODIGO = "1.0.0"

PARES = paths.E6A_PARES
CATALOGO = paths.E0A_CATALOGO
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
SAIDA = paths.E6B_RERANKEADOS

COLS = ["par_key", "score_rerank", "decisao"]


class Params(BaseModel):
    limite: int | None = Field(None, description="Teto de pares a rerankear (debug)")
    batch: int = Field(16, ge=1, description="Tamanho do lote enviado ao reranker")
    remoto: bool = Field(
        False, description="Usa o servidor de GPU (GPU_BASE_URL) para o reranker")
    fonte: Literal["banco", "csv"] = Field(
        "banco", description="De onde vêm os pares e para onde vai a decisão")


# ── Caminho `--fonte banco` (Fase 10) ───────────────────────────────────────────────
#
# Os pares e os textos vêm do banco; a decisão volta para as MESMAS linhas de `par` (ADR-013:
# uma tabela, não três). A chave de resumo deixa de ser "par_key já no CSV" e passa a ser
# `par.score_rerank IS NULL` — derivada do próprio dado, como manda o ADR-018.

def _exigir_banco():
    from pesquisa_precos.db import sessao as db

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env "
                         f"ou rode com --fonte csv.")
    return db


def executar_no_banco(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import par as repo_par

    cfg = ctx.config
    limite = f" LIMIT {int(params.limite)}" if params.limite else ""
    with db.sessao() as s:
        linhas = s.execute(sa_text(f"""
            SELECT p.par_key,
                   trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.descricao, '')),
                   coalesce(NULLIF(e.descricao_final, ''), i.descricao_api)
              FROM par p
              JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
              JOIN item i ON i.item_key = p.item_key
              LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
             WHERE p.sobreviveu AND p.score_rerank IS NULL
             ORDER BY p.par_key{limite}
        """)).all()

    if not linhas:
        ctx.log("info", "[6b] Nada a rerankear (todo par sobrevivente já tem score).")
        return ResultadoEtapa()

    par_keys = [l[0] for l in linhas]
    pares_txt = [(l[1] or "", l[2] or "") for l in linhas]
    ctx.log("info", f"[6b] Rerankeando {len(pares_txt)} pares do banco"
                    f"{' (remoto)' if params.remoto else ''}...")
    rer = ctx.provedores.novo_rerank(remoto=params.remoto, batch=params.batch)

    passo = max(params.batch, 256)
    decisoes: dict[str, int] = {"aceito": 0, "rejeitado": 0, "ambiguo": 0}
    try:
        ctx.progresso(0, len(pares_txt), descricao="rerankeando")
        for i in range(0, len(pares_txt), passo):
            if ctx.cancelado():
                break
            scores = rer.score_pares(pares_txt[i:i + passo])
            lote = []
            for par_key, score in zip(par_keys[i:i + passo], scores):
                decisao = decidir(float(score), cfg["rerank_t_aceita"],
                                  cfg["rerank_t_rejeita"])
                decisoes[decisao] += 1
                lote.append((par_key, round(float(score), 4), decisao))
            # Grava a cada bloco, não no fim: o reranker é caro em GPU e uma queda no meio
            # não pode custar o que já foi calculado.
            with db.sessao() as s:
                repo_par.gravar_rerank(s, lote)
                s.commit()
            ctx.progresso(min(i + passo, len(pares_txt)))
    finally:
        rer.liberar()

    with db.sessao() as s:
        contagens = repo_par.contar(s)
    ctx.log("info", f"[bold green][6b] Concluído.[/] {decisoes} → tabela `par`")
    return ResultadoEtapa(processados=sum(decisoes.values()), erros=0,
                          metricas={**decisoes, **contagens})


def decidir(score: float, t_aceita: float, t_rejeita: float) -> str:
    if score >= t_aceita:
        return "aceito"
    if score <= t_rejeita:
        return "rejeitado"
    return "ambiguo"


def _pares_pendentes(params: Params) -> pd.DataFrame:
    df = pd.read_csv(PARES, dtype=str, encoding="utf-8").fillna("")
    df = df[df["sobreviveu"].str.lower().isin(["true", "1", "verdadeiro"])]
    feitas = ler_chaves_concluidas(str(SAIDA), "par_key")
    df = df[~df["par_key"].isin(feitas)]
    if params.limite:
        df = df.head(params.limite)
    return df


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Pares a rerankear. Roda na GPU: custo de token zero."""
    if params.fonte == "banco":
        from pesquisa_precos.db import sessao as db

        ok, detalhe = db.esta_disponivel()
        if not ok:
            return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
        with db.sessao() as s:
            n = s.execute(sa_text(
                "SELECT count(*) FROM par WHERE sobreviveu AND score_rerank IS NULL")
            ).scalar_one()
        return Estimativa(
            unidades=n, chamadas_llm=0, custo_usd=0.0,
            detalhes={"fonte": "banco", "lotes": -(-n // max(params.batch, 1)),
                      "reranker": "GPU remota" if params.remoto else "local"},
        )

    if not PARES.exists():
        return Estimativa(detalhes={"aviso": f"{PARES} ausente — rode a etapa 6a antes."})
    df = _pares_pendentes(params)
    return Estimativa(
        unidades=len(df), chamadas_llm=0, custo_usd=0.0,
        detalhes={"lotes": -(-len(df) // max(params.batch, 1)),
                  "reranker": "GPU remota" if params.remoto else "local"},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    if params.fonte == "banco":
        return executar_no_banco(params, ctx)

    cfg = ctx.config
    if not PARES.exists():
        raise SystemExit(f"{PARES} ausente. Rode a etapa 6a antes.")

    df = _pares_pendentes(params)
    if df.empty:
        ctx.log("info", "[6b] Nada a rerankear.")
        return ResultadoEtapa()

    cat = texto_catalogo(str(CATALOGO))
    itens = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))

    pares_txt, registros = [], []
    for _, r in df.iterrows():
        t_cat = cat.get(r["codigo"], {}).get("texto", "")
        t_itm = itens.get(r["item_key"], "")
        pares_txt.append((t_cat, t_itm))
        registros.append(r["par_key"])

    ctx.log("info", f"[6b] Rerankeando {len(pares_txt)} pares"
                    f"{' (remoto)' if params.remoto else ''}...")
    # Fase 7 (ADR-006): banco (`capacidade_provedor`) manda se configurado; `--remoto` continua
    # valendo como override manual no caminho `.env`. Fallback é PERMITIDO em rerank.
    rer = ctx.provedores.novo_rerank(remoto=params.remoto, batch=params.batch)

    # Processa em blocos para a barra de progresso avançar (local e remoto).
    passo = max(params.batch, 256)
    decisoes: dict[str, int] = {"aceito": 0, "rejeitado": 0, "ambiguo": 0}
    with EscritorSeguro(str(SAIDA), COLS) as w:
        ctx.progresso(0, len(pares_txt), descricao="rerankeando")
        for i in range(0, len(pares_txt), passo):
            if ctx.cancelado():
                break
            bloco = pares_txt[i:i + passo]
            scores = rer.score_pares(bloco)
            for par_key, score in zip(registros[i:i + passo], scores):
                decisao = decidir(float(score), cfg["rerank_t_aceita"], cfg["rerank_t_rejeita"])
                decisoes[decisao] += 1
                w.escrever({"par_key": par_key, "score_rerank": round(float(score), 4),
                            "decisao": decisao})
            ctx.progresso(min(i + passo, len(pares_txt)))
    rer.liberar()
    ctx.log("info", f"[6b] Concluído. Saída: {SAIDA}")

    return ResultadoEtapa(
        processados=sum(decisoes.values()), erros=0,
        metricas={**decisoes, "t_aceita": cfg["rerank_t_aceita"],
                  "t_rejeita": cfg["rerank_t_rejeita"]},
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
