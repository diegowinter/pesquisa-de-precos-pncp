"""
Etapa 6b — Reranker local (cross-encoder) decide a maioria dos pares, custo zero de token.

Para cada par sobrevivente da 6a: score do cross-encoder bge-reranker sobre (texto_catalogo,
descricao_final do item). Decisão por threshold:
  score >= RERANK_T_ACEITA → aceito;  score <= RERANK_T_REJEITA → rejeitado;  entre → ambiguo.

Entrada: data/6a_pares_candidatos.csv (sobreviveu=true) + textos das saídas anteriores.
Saída: data/6b_pares_rerankeados.csv (par_key, score_rerank, decisao). Chave de resumo: par_key.
GPU: o reranker roda sozinho.
Uso: python -m pesquisa_precos.etapas.e6b_rerank [--limite N]
"""

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo

PARES = paths.E6A_PARES
CATALOGO = paths.E0A_CATALOGO
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
SAIDA = paths.E6B_RERANKEADOS

COLS = ["par_key", "score_rerank", "decisao"]


def decidir(score: float, t_aceita: float, t_rejeita: float) -> str:
    if score >= t_aceita:
        return "aceito"
    if score <= t_rejeita:
        return "rejeitado"
    return "ambiguo"


def main():
    ap = argparse.ArgumentParser(description="Etapa 6b — reranker local")
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--remoto", action="store_true",
                    help="usa o servidor de GPU (GPU_BASE_URL) para o reranker, em vez do local")
    args = ap.parse_args()

    cfg = carregar_config()
    if not PARES.exists():
        raise SystemExit(f"{PARES} ausente. Rode a etapa 6a antes.")

    df = pd.read_csv(PARES, dtype=str, encoding="utf-8").fillna("")
    df = df[df["sobreviveu"].str.lower().isin(["true", "1", "verdadeiro"])]
    feitas = ler_chaves_concluidas(str(SAIDA), "par_key")
    df = df[~df["par_key"].isin(feitas)]
    if args.limite:
        df = df.head(args.limite)
    if df.empty:
        print("[6b] Nada a rerankear.")
        return

    cat = texto_catalogo(str(CATALOGO))
    itens = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))

    pares_txt, registros = [], []
    for _, r in df.iterrows():
        t_cat = cat.get(r["codigo"], {}).get("texto", "")
        t_itm = itens.get(r["item_key"], "")
        pares_txt.append((t_cat, t_itm))
        registros.append(r["par_key"])

    print(f"[6b] Rerankeando {len(pares_txt)} pares{' (remoto)' if args.remoto else ''}...")
    if args.remoto:
        from pesquisa_precos.providers.gpu_remoto import RerankerRemoto
        rer = RerankerRemoto(cfg["gpu_base_url"], cfg["gpu_api_key"], batch=args.batch)
    else:
        from pesquisa_precos.providers.reranker_local import RerankerLocal
        rer = RerankerLocal(cfg["reranker_model"], batch=args.batch)

    # Processa em blocos para a barra de progresso avançar (local e remoto).
    passo = max(args.batch, 256)
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
        MofNCompleteColumn(), TimeElapsedColumn(), console=Console(),
    )
    with progress, EscritorSeguro(str(SAIDA), COLS) as w:
        tarefa = progress.add_task("rerankeando", total=len(pares_txt))
        for i in range(0, len(pares_txt), passo):
            bloco = pares_txt[i:i + passo]
            scores = rer.score_pares(bloco)
            for par_key, score in zip(registros[i:i + passo], scores):
                w.escrever({"par_key": par_key, "score_rerank": round(float(score), 4),
                            "decisao": decidir(float(score), cfg["rerank_t_aceita"], cfg["rerank_t_rejeita"])})
            progress.update(tarefa, completed=min(i + passo, len(pares_txt)))
    rer.liberar()
    print(f"[6b] Concluído. Saída: {SAIDA}")


if __name__ == "__main__":
    main()
