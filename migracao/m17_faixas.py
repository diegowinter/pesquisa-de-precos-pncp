"""
m17 — Faixas de preço: `config_faixas_preco.csv` → `faixa_preco`.

Arquivo pequeno e **curado à mão**: por categoria, o preço mínimo e máximo plausível. A step 7
o usa para sinalizar preço fora da faixa, ao lado do outlier por IQR. Limite vazio significa
"sem limite deste lado" (`arma_fogo,5,` = mínimo 5, sem teto) — vira NULL, não zero. Confundir
os dois transformaria "sem teto" em "teto zero" e sinalizaria a categoria inteira.

Uso: python -m migracao.m17_faixas
"""

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import grupo as repo
from migracao._comum import Relatorio, cabecalho, console, dec, existe, ler_csv


def migrar() -> Relatorio:
    rel = Relatorio("m17 — faixas de preço")
    if not existe(paths.FAIXAS_PRECO):
        rel.aviso(f"{paths.FAIXAS_PRECO.name} ausente — a step 7 aplica só o corte por IQR.")
        return rel

    faixas = []
    for r in ler_csv(paths.FAIXAS_PRECO):
        categoria = (r.get("categoria") or "").strip()
        if not categoria:
            rel.mais("linhas sem categoria")
            continue
        faixas.append((categoria, dec(r.get("preco_min")), dec(r.get("preco_max"))))
        rel.mais("linhas lidas")

    with db.session() as s:
        rel.mais("faixas gravadas", repo.gravar_faixas(s, faixas))
        rel.mais("faixa_preco no banco", repo.contar(s)["faixa_preco"])
    return rel


def main() -> None:
    cabecalho("m17 — faixas de preço", paths.FAIXAS_PRECO, "faixa_preco")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
