"""
Etapa 7 — Agrupar por código, sanity de preço, regra dos 5 (definitiva), top 5 mais baratos.

Confirmados = pares `aceito` da 6b ∪ pares `mesmo_item=sim` da 6c. Antes do ranking, marca
outliers de preço por IQR (por código): preço < Q1-3*IQR ou > Q3+3*IQR → flag_preco=true
(fica no arquivo mas fora do top 5 — um erro de unidade não pode contaminar a pesquisa). Se
existir data/config_faixas_preco.csv (categoria, preco_min, preco_max), aplica também. Regra
dos 5: por código, conta confirmados não-flagados; < MIN_ITENS descarta o código. Nos que
fecham, mantém os TOP_N mais baratos por preço unitário.

Saídas: data/7_itens_agrupados.csv, data/7_relatorio_grupos.csv.
Uso: python -m pesquisa_precos.etapas.e7_agrupar
"""

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import numpy as np
import pandas as pd

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo

RERANK = paths.E6B_RERANKEADOS
VALIDADOS = paths.E6C_VALIDADOS
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
CATALOGO = paths.E0A_CATALOGO
PARES = paths.E6A_PARES
FAIXAS = paths.FAIXAS_PRECO
AGRUPADOS = paths.E7_AGRUPADOS
RELATORIO = paths.E7_RELATORIO

META_ITEM = ["tipo_doc", "numeroControlePNCP", "numeroItem", "unidade", "quantidade",
             "preco_unitario", "preco_estimado", "fornecedor", "data_resultado",
             "orgao", "uf", "data", "ano", "orgao_cnpj",
             "data_fim_vigencia", "data_assinatura"]


def confirmados() -> pd.DataFrame:
    if not RERANK.exists():
        raise SystemExit(f"{RERANK} ausente. Rode a etapa 6b antes.")
    rer = pd.read_csv(RERANK, dtype=str, encoding="utf-8").fillna("")
    keys = set(rer[rer["decisao"] == "aceito"]["par_key"])
    if VALIDADOS.exists():
        val = pd.read_csv(VALIDADOS, dtype=str, encoding="utf-8").fillna("")
        keys |= set(val[val["mesmo_item"] == "sim"]["par_key"])
    cat_map = {}
    if PARES.exists():
        pares = pd.read_csv(PARES, dtype=str, encoding="utf-8").fillna("")
        cat_map = dict(zip(pares["par_key"], pares["categoria"]))
    linhas = []
    for pk in keys:
        codigo, item_key = pk.split("::", 1)
        linhas.append({"par_key": pk, "codigo": codigo, "item_key": item_key,
                       "categoria": cat_map.get(pk, "")})
    return pd.DataFrame(linhas)


def flag_iqr(precos: pd.Series) -> pd.Series:
    p = pd.to_numeric(precos, errors="coerce")
    q1, q3 = p.quantile(0.25), p.quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
    # preço ausente ou <= 0 é lixo (não preenchido no PNCP): flaga sempre, mesmo que o IQR não pegue.
    return (p.isna()) | (p <= 0) | (p < lo) | (p > hi)


def main():
    ap = argparse.ArgumentParser(description="Etapa 7 — agrupar top5")
    args = ap.parse_args()
    cfg = carregar_config()
    min_itens, top_n = cfg["min_itens"], cfg["top_n"]

    conf = confirmados()
    if conf.empty:
        raise SystemExit("Nenhum par confirmado (aceito/sim). Rode 6b/6c antes.")

    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    meta_cols = ["item_key"] + [c for c in META_ITEM if c in sob.columns]
    conf = conf.merge(sob[meta_cols], on="item_key", how="left")

    desc = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))
    conf["descricao_final"] = conf["item_key"].map(desc).fillna("")
    catt = texto_catalogo(str(CATALOGO))
    conf["nome_catalogo"] = conf["codigo"].map(lambda c: catt.get(c, {}).get("nome", ""))

    conf["preco_num"] = pd.to_numeric(conf.get("preco_unitario"), errors="coerce")
    conf["flag_preco"] = conf.groupby("codigo")["preco_unitario"].transform(flag_iqr)

    # faixas manuais opcionais por categoria
    if FAIXAS.exists():
        fx = pd.read_csv(FAIXAS, dtype=str, encoding="utf-8").fillna("")
        faixa = {r["categoria"]: (float(r["preco_min"] or "nan"), float(r["preco_max"] or "nan")) for _, r in fx.iterrows()}

        def fora_faixa(row):
            lo, hi = faixa.get(row["categoria"], (np.nan, np.nan))
            p = row["preco_num"]
            return bool((not np.isnan(lo) and p < lo) or (not np.isnan(hi) and p > hi))
        conf["flag_preco"] = conf["flag_preco"] | conf.apply(fora_faixa, axis=1)

    n_flag_total = int(conf["flag_preco"].sum())
    print(f"[7] Piso/IQR cortou {n_flag_total} de {len(conf)} confirmados por preço "
          f"absurdo/ausente ({'com' if FAIXAS.exists() else 'sem'} config_faixas_preco.csv).")

    validos = conf[~conf["flag_preco"]].copy()
    contagem = validos.groupby("codigo")["item_key"].nunique()
    fecham = set(contagem[contagem >= min_itens].index)

    # Ordena por preço (mais baratos primeiro). top_n<=0 → traz TODAS as referências
    # confirmadas não-lixo por código (sem teto); top_n>0 → só as N mais baratas.
    selec = (
        validos[validos["codigo"].isin(fecham)]
        .sort_values("preco_num", kind="mergesort")
    )
    if top_n and top_n > 0:
        selec = selec.groupby("codigo", group_keys=False).head(top_n)
    selec.drop(columns=["preco_num"], errors="ignore").to_csv(AGRUPADOS, index=False, encoding="utf-8")

    relatorio = pd.DataFrame({
        "codigo": contagem.index,
        "n_confirmados": contagem.values,
    })
    n_flag = conf[conf["flag_preco"]].groupby("codigo")["item_key"].nunique()
    relatorio["n_flagados"] = relatorio["codigo"].map(n_flag).fillna(0).astype(int)
    relatorio["fechou"] = relatorio["codigo"].isin(fecham)
    relatorio.sort_values("n_confirmados", ascending=False).to_csv(RELATORIO, index=False, encoding="utf-8")

    teto = "sem teto" if not top_n or top_n <= 0 else f"top{top_n}"
    print(f"[7] Códigos que fecharam ({min_itens}+): {len(fecham)} | linhas ({teto}): {len(selec)}")
    print(f"[7] Saída: {AGRUPADOS} | relatório: {RELATORIO}")


if __name__ == "__main__":
    main()
