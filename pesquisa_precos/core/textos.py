"""
Mapas de texto compartilhados pelas etapas de pareamento (6b, 6c, 7, 8).

Reúne num só lugar como montar o texto do item de catálogo e do item PNCP (com a descrição
enriquecida do PDF quando houver), evitando duplicação entre os scripts numerados (que não
podem importar uns aos outros).
"""

import os

import pandas as pd


def texto_catalogo(caminho_catalogo: str) -> dict[str, dict]:
    """codigo → {texto, nome, descricao, categoria?} a partir de 0a_catalogo_filtrado.csv."""
    df = pd.read_csv(caminho_catalogo, dtype=str, encoding="utf-8-sig").fillna("")
    out = {}
    for _, r in df.iterrows():
        texto = (r.get("nome_pdm", "") + " " + r.get("descricao", "")).strip()
        out[r["codigo"]] = {"texto": texto, "nome": r.get("nome_pdm", ""),
                            "descricao": r.get("descricao", "")}
    return out


def descricao_itens(caminho_sobreviventes: str, caminho_enriquecidos: str | None = None) -> dict[str, str]:
    """item_key → descrição final (enriquecida do PDF quando disponível, senão a da API)."""
    df = pd.read_csv(caminho_sobreviventes, dtype=str, encoding="utf-8").fillna("")
    out = {r["item_key"]: r.get("descricao_api", "") for _, r in df.iterrows()}
    if caminho_enriquecidos and os.path.exists(caminho_enriquecidos) and os.path.getsize(caminho_enriquecidos) > 0:
        enr = pd.read_csv(caminho_enriquecidos, dtype=str, encoding="utf-8").fillna("")
        for _, r in enr.iterrows():
            if r.get("descricao_final"):
                out[r["item_key"]] = r["descricao_final"]
    return out
