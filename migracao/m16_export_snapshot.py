"""
m16 — Snapshot do `--novos`: `8_export_snapshot.csv` → `export_snapshot`.

Este passo existe para desarmar uma armadilha específica: a primeira execução do `--novos` sem
snapshot prévio marca **TUDO** como novidade — 118 mil linhas que o cliente já recebeu. Isso
não é bug; é o comportamento correto de um delta sem baseline. A correção é semear o baseline a
partir do último export **oficial**, que é exatamente o que este script faz
(docs/05_MIGRACAO.md §6.3, docs/02_SCHEMA.md §8).

O CSV tem BOM (`utf-8-sig`) e três colunas: `codigo, numeroControlePNCP, numeroItem`. Falta o
`tipo`, que a PK do destino exige — resolved pelo catálogo, com a mesma validação de
ambiguidade do m12.

Uso: python -m migracao.m16_export_snapshot
"""

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import catalogo as repo_cat
from pesquisa_precos.db.repos import grupo as repo
from migracao._comum import Relatorio, cabecalho, console, existe, inteiro, ler_csv


def migrar() -> Relatorio:
    rel = Relatorio("m16 — snapshot do export")
    if not existe(paths.E8_SNAPSHOT):
        rel.aviso(f"{paths.E8_SNAPSHOT.name} ausente — sem baseline, o primeiro `--novos` do "
                  f"sistema novo vai marcar TODAS as linhas como novidade. Gere o snapshot "
                  f"a partir do último export oficial antes de rodar a step 8 com --novos.")
        return rel

    with db.session() as s:
        tipo_de, ambiguos = repo_cat.tipo_do_codigo(s)
    if ambiguos:
        raise SystemExit(f"ABORTADO: códigos ambíguos no catálogo "
                         f"({', '.join(ambiguos[:5])}...) — ver m12.")

    chaves = []
    for r in ler_csv(paths.E8_SNAPSHOT, encoding="utf-8-sig"):
        codigo = (r.get("codigo") or "").strip()
        nc = (r.get("numeroControlePNCP") or "").strip()
        numero = inteiro(r.get("numeroItem"))
        if not (codigo and nc and numero is not None):
            rel.mais("linhas incompletas")
            continue
        tipo = tipo_de.get(codigo)
        if tipo is None:
            rel.mais("linhas com código fora do catálogo (descartadas)")
            continue
        chaves.append((tipo, codigo, nc, numero))
        rel.mais("linhas lidas")

    with db.raw_connection() as conn:
        # `substituir=True`: o snapshot é um RETRATO do último export, não um acumulado.
        rel.mais("chaves gravadas", repo.avancar_snapshot(conn, chaves, None, substituir=True))

    with db.session() as s:
        rel.mais("export_snapshot no banco", repo.contar(s)["export_snapshot"])
    return rel


def main() -> None:
    cabecalho("m16 — snapshot do export", paths.E8_SNAPSHOT, "export_snapshot")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
