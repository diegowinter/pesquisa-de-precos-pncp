"""
Etapa 4 — Junção classificação × metadados + filtro de classificados (pandas puro, sem LLM).

Junta a classificação (etapa 3, só itens com ≥1 categoria) com os itens coletados, explode
o multi-label (1 linha por (item_key, categoria)) e reagrega. Mantém TODAS as caixas — a
antiga regra dos 5 (descartar categoria com < MIN_ITENS) foi removida. O único filtro é
"item tem ≥1 categoria de conteúdo"; a contagem por caixa vira apenas diagnóstico.

Entradas: data/2_itens_coletados.csv, data/3_itens_classificados.csv.
Saídas: data/4_itens_sobreviventes.csv (colunas da etapa 2 + categorias do item),
         data/4_relatorio_corte.csv (categoria, n_itens_coletados, mantida≡True) — diagnóstico.
Uso: python 4_cortar_minimo.py
"""

import argparse
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd

from scripts import coleta_pncp

DATA = Path(__file__).resolve().parent / "data"
ITENS = DATA / "2_itens_coletados.csv"
CK_EXTRA = DATA / "checkpoints" / "2_conceitos_extra.csv"
CLASSIF = DATA / "3_itens_classificados.csv"
SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"
RELATORIO = DATA / "4_relatorio_corte.csv"


def main():
    ap = argparse.ArgumentParser(description="Etapa 4 — corte antecipado (regra dos 5)")
    ap.add_argument("--entrada-legado", default=None, help="CSV explodido da v1 (aceite fase 2)")
    args = ap.parse_args()

    if args.entrada_legado:
        base = pd.read_csv(args.entrada_legado, dtype=str, encoding="utf-8-sig").fillna("")
        ctrl = base.get("numero_controle_pncp", "")
        num = base.get("item.numero_item", "")
        base = base.assign(item_key=[coleta_pncp.montar_item_key(c, n) for c, n in zip(ctrl, num)])
        base = base.drop_duplicates(subset="item_key", keep="first")
    else:
        base = coleta_pncp.carregar_itens_coletados(str(ITENS), str(CK_EXTRA))
    if base.empty:
        raise SystemExit("Sem itens coletados. Rode a etapa 2 antes (ou use --entrada-legado).")

    if not CLASSIF.exists():
        raise SystemExit(f"{CLASSIF} ausente. Rode a etapa 3 antes.")
    clas = pd.read_csv(CLASSIF, dtype=str, encoding="utf-8").fillna("")
    clas = clas[clas["categorias"].str.strip() != ""]

    # explode multi-label
    clas = clas.assign(categoria=clas["categorias"].str.split("|")).explode("categoria")
    clas = clas[clas["categoria"].str.strip() != ""]

    contagem = clas.groupby("categoria")["item_key"].nunique()
    # Regra dos 5 removida: mantém TODAS as caixas classificadas, independente da contagem.
    # O único filtro aqui é "item tem ≥1 caixa" (feito acima). A contagem vira só diagnóstico.
    mantidas = set(contagem.index)

    relatorio = (
        contagem.rename("n_itens_coletados").reset_index()
        .assign(mantida=lambda d: d["categoria"].isin(mantidas))
        .sort_values("n_itens_coletados", ascending=False)
    )
    relatorio.to_csv(RELATORIO, index=False, encoding="utf-8")

    # sobreviventes: itens com ≥1 categoria mantida; categorias filtradas às mantidas.
    itens_cat = (
        clas[clas["categoria"].isin(mantidas)]
        .groupby("item_key")["categoria"].apply(lambda s: "|".join(sorted(set(s))))
        .rename("categorias").reset_index()
    )
    base_cols = [c for c in base.columns if c != "categorias"]
    sobrev = itens_cat.merge(base[base_cols], on="item_key", how="left")
    sobrev.to_csv(SOBREVIVENTES, index=False, encoding="utf-8")

    print(f"[4] Categorias mantidas: {sorted(mantidas)}")
    print(f"[4] Itens sobreviventes: {len(sobrev)} | relatório: {RELATORIO}")
    print(f"[4] Saída: {SOBREVIVENTES}")


if __name__ == "__main__":
    main()
