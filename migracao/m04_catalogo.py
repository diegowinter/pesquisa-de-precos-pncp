"""
m04 — Catálogo: `0a_catalogo_filtrado.csv` + `1_categoria_por_codigo.csv` → `catalogo_item`.

A `categoria` NÃO vem do catálogo: vem de `1_categoria_por_codigo.csv`, produzido pela etapa 1.
É a fonte canônica por item usada pelo pareamento da 6a — o join é feito aqui, na migração, e
não em cada consulta depois (docs/05_MIGRACAO.md §m04).

`0a_catalogo_filtrado.csv` tem BOM: precisa de `utf-8-sig`. Sem isso a primeira coluna do
cabeçalho vem como '\\ufefftipo' e todo `row["tipo"]` estoura.

`0a_catalogo_delta.csv` marca códigos `removido` → `active = false`. Desativa, nunca apaga: o
código removido do CATMAT continua sendo a origem de linhas já entregues em export.

Uso: python -m migracao.m04_catalogo
"""

from sqlalchemy import text as sql

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import catalogo as repo
from migracao._comum import Relatorio, cabecalho, console, existe, ler_csv, txt


def categoria_por_codigo() -> dict[tuple[str, str], str]:
    """(tipo, codigo) → categoria. Chaveado pelos DOIS, porque o código só é único no tipo."""
    if not existe(paths.E1_CATEGORIA_POR_CODIGO):
        return {}
    return {
        ((r.get("tipo") or "").strip(), (r.get("codigo") or "").strip()):
            (r.get("categoria") or "").strip()
        for r in ler_csv(paths.E1_CATEGORIA_POR_CODIGO)
        if (r.get("codigo") or "").strip()
    }


def removidos() -> list[tuple[str, str]]:
    if not existe(paths.E0A_DELTA):
        return []
    return [((r.get("tipo") or "").strip(), (r.get("codigo") or "").strip())
            for r in ler_csv(paths.E0A_DELTA, encoding="utf-8-sig")
            if (r.get("status") or "").strip() == "removido"]


def migrar() -> Relatorio:
    rel = Relatorio("m04 — catálogo")
    if not existe(paths.E0A_CATALOGO):
        raise SystemExit(f"{paths.E0A_CATALOGO} ausente. Rode a step 0a antes.")

    cats = categoria_por_codigo()
    rel.mais("categorias mapeadas", len(cats))

    def linhas():
        for r in ler_csv(paths.E0A_CATALOGO, encoding="utf-8-sig"):
            tipo = (r.get("tipo") or "").strip()
            codigo = (r.get("codigo") or "").strip()
            if not (tipo and codigo):
                rel.mais("linhas sem tipo/codigo")
                continue
            rel.mais("linhas lidas")
            yield (tipo, codigo, txt(r.get("codigo_pdm")), txt(r.get("nome_pdm")),
                   r.get("description") or "", txt(r.get("codigo_grupo")),
                   txt(r.get("nome_grupo")), txt(r.get("nome_classe")),
                   cats.get((tipo, codigo)), True)

    with db.raw_connection() as conn:
        for lote in em_lotes(linhas(), 5_000):
            rel.mais("gravadas", repo.gravar_itens(conn, lote))

    inativos = removidos()
    with db.session() as s:
        rel.mais("marcados inativos (delta 'removido')", repo.marcar_inativos(s, inativos))
        rel.mais("total no banco", repo.contar(s))
        sem_categoria = s.execute(sql(
            "SELECT count(*) FROM catalogo_item "
            "WHERE categoria IS NULL OR categoria = ''")).scalar_one()
    if sem_categoria:
        # Código sem categoria não pareia na 6a (o produto é restrito à mesma categoria).
        # Não é erro de migração — é buraco da etapa 1 —, mas precisa aparecer.
        rel.aviso(f"{sem_categoria} códigos ficaram SEM categoria — eles não pareiam na 6a.")
    return rel


def main() -> None:
    cabecalho("m04 — catálogo", [paths.E0A_CATALOGO, paths.E1_CATEGORIA_POR_CODIGO,
                                 paths.E0A_DELTA], "catalogo_item")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
