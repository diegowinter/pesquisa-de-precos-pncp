"""
Calibração de thresholds do funil (seção 6 do guia). Duas fases:

  --amostrar   estratifica ~N pares de data/6a_pares_candidatos.csv por faixa de score e
               exporta ferramentas/amostra_rotulagem.csv com coluna `rotulo_humano` vazia
               (preencher à mão: sim/nao).
  --analisar   com a amostra preenchida, reporta:
               - rejeitor (6a): maior REJEITOR_THRESHOLD que mantém recall ≥ 99% dos `sim`;
               - reranker (6b): curva precisão/recall por threshold, sugestão de T_ACEITA
                 (precisão ≥ 97% nos aceitos) e T_REJEITA (recall ≥ 99% preservado acima),
                 e o % de pares que sobra como ambíguo (custo de LLM esperado).

Uso: python ferramentas/calibrar_thresholds.py --amostrar [--n 180]
     python ferramentas/calibrar_thresholds.py --analisar
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
PARES = RAIZ / "data" / "6a_pares_candidatos.csv"
RERANK = RAIZ / "data" / "6b_pares_rerankeados.csv"
AMOSTRA = Path(__file__).resolve().parent / "amostra_rotulagem.csv"


def amostrar(n: int) -> None:
    if not PARES.exists():
        raise SystemExit(f"{PARES} ausente. Rode a etapa 6a antes.")
    df = pd.read_csv(PARES, dtype=str).fillna("")
    df["score"] = pd.to_numeric(df["score_cosseno"], errors="coerce").fillna(0.0)
    df = df[df["sobreviveu"].str.lower().isin(["true", "1"])].copy()
    if df.empty:
        raise SystemExit("Nenhum par sobrevivente para amostrar.")
    # estratifica por faixa de score (5 faixas)
    df["faixa"] = pd.cut(df["score"], bins=5, labels=False, include_lowest=True)
    por_faixa = max(1, n // 5)
    amostra = (df.groupby("faixa", group_keys=False)
               .apply(lambda g: g.sample(min(len(g), por_faixa), random_state=42)))
    amostra = amostra[["par_key", "codigo", "item_key", "categoria", "score_bm25", "score_cosseno"]].copy()
    amostra["rotulo_humano"] = ""
    amostra.to_csv(AMOSTRA, index=False, encoding="utf-8")
    print(f"[calibrar] {len(amostra)} pares → {AMOSTRA} (preencha rotulo_humano com sim/nao)")


def _carregar_rotulada() -> pd.DataFrame:
    if not AMOSTRA.exists():
        raise SystemExit(f"{AMOSTRA} ausente. Rode --amostrar antes.")
    df = pd.read_csv(AMOSTRA, dtype=str).fillna("")
    df = df[df["rotulo_humano"].str.strip().str.lower().isin(["sim", "nao"])].copy()
    if df.empty:
        raise SystemExit("Amostra sem rótulos preenchidos (sim/nao).")
    df["y"] = (df["rotulo_humano"].str.strip().str.lower() == "sim").astype(int)
    return df


def analisar_rejeitor(df: pd.DataFrame) -> None:
    df = df.copy()
    df["sinal"] = np.maximum(
        pd.to_numeric(df["score_bm25"], errors="coerce").fillna(0),
        pd.to_numeric(df["score_cosseno"], errors="coerce").fillna(0),
    )
    positivos = df[df["y"] == 1]
    if positivos.empty:
        print("[rejeitor] sem positivos rotulados."); return
    melhor = 0.0
    for t in np.linspace(0, 1, 101):
        recall = (positivos["sinal"] >= t).mean()
        if recall >= 0.99:
            melhor = t
    print(f"[rejeitor 6a] maior REJEITOR_THRESHOLD com recall≥99% dos 'sim': {melhor:.2f}")


def analisar_reranker(df: pd.DataFrame) -> None:
    if not RERANK.exists():
        print("[reranker] 6b_pares_rerankeados.csv ausente — pule para calibrar 6b."); return
    rer = pd.read_csv(RERANK, dtype=str).fillna("")
    rer["score_rerank"] = pd.to_numeric(rer["score_rerank"], errors="coerce")
    m = df.merge(rer[["par_key", "score_rerank"]], on="par_key", how="inner").dropna(subset=["score_rerank"])
    if m.empty:
        print("[reranker] sem interseção amostra×6b."); return
    print("[reranker 6b] threshold | precisao_aceitos | recall_positivos")
    t_aceita, t_rejeita = None, None
    for t in np.linspace(0, 1, 21):
        aceitos = m[m["score_rerank"] >= t]
        prec = (aceitos["y"] == 1).mean() if len(aceitos) else float("nan")
        recall = (m[m["y"] == 1]["score_rerank"] >= t).mean()
        print(f"   {t:.2f}      {prec:.3f}            {recall:.3f}")
        if t_aceita is None and prec >= 0.97 and len(aceitos):
            t_aceita = t
    for t in np.linspace(1, 0, 21):
        recall = (m[m["y"] == 1]["score_rerank"] >= t).mean()
        if recall >= 0.99:
            t_rejeita = t
    if t_aceita is not None:
        print(f"[reranker] sugestão T_ACEITA ≈ {t_aceita:.2f} (precisão≥97%)")
    if t_rejeita is not None:
        print(f"[reranker] sugestão T_REJEITA ≈ {t_rejeita:.2f} (recall≥99% acima dele)")
        if t_aceita is not None:
            ambiguos = ((m["score_rerank"] > t_rejeita) & (m["score_rerank"] < t_aceita)).mean()
            print(f"[reranker] % ambíguo estimado (custo LLM): {ambiguos*100:.1f}%")


def main():
    ap = argparse.ArgumentParser(description="Calibração de thresholds do funil")
    ap.add_argument("--amostrar", action="store_true")
    ap.add_argument("--analisar", action="store_true")
    ap.add_argument("--n", type=int, default=180, help="Tamanho da amostra (--amostrar)")
    args = ap.parse_args()
    if not (args.amostrar or args.analisar):
        ap.error("Use --amostrar ou --analisar.")
    if args.amostrar:
        amostrar(args.n)
    if args.analisar:
        df = _carregar_rotulada()
        analisar_rejeitor(df)
        analisar_reranker(df)


if __name__ == "__main__":
    main()
