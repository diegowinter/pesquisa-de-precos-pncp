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

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
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
    if not PARES.exists():
        return Estimativa(detalhes={"aviso": f"{PARES} ausente — rode a etapa 6a antes."})
    df = _pares_pendentes(params)
    return Estimativa(
        unidades=len(df), chamadas_llm=0, custo_usd=0.0,
        detalhes={"lotes": -(-len(df) // max(params.batch, 1)),
                  "reranker": "GPU remota" if params.remoto else "local"},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
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
