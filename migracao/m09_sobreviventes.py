"""
m09 — Sobreviventes: `4_itens_sobreviventes.csv` → `item.sobrevivente` +
`documento.n_itens_sobreviventes`.

O CSV da etapa 4 é o corpus inteiro filtrado (190 MB, 302.514 linhas) e repete todas as colunas
do item. Aqui só interessa a CHAVE: quem sobreviveu ao corte. Nada mais é migrado deste arquivo
— os dados do item já vieram do m07, e reimportá-los daqui abriria a chance de duas versões da
mesma linha no banco.

`documento.n_itens_sobreviventes` é derivado e recomputado por SQL puro no fim. Não é lido do
CSV: contar no banco é mais barato e não pode divergir do que está lá.

Uso: python -m migracao.m09_sobreviventes [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import documento as repo
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    existe,
    ler_csv,
)

LOTE = 20_000


def migrar(reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m09 — sobreviventes")
    if not existe(paths.E4_SOBREVIVENTES):
        raise SystemExit(f"{paths.E4_SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    retomada = Retomada.carregar("m09_sobreviventes")
    if reiniciar:
        retomada.zerar()

    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E4_SOBREVIVENTES)
    rel.mais("registros no CSV (estimado)", total)

    def chaves():
        for i, r in enumerate(ler_csv(paths.E4_SOBREVIVENTES), 1):
            if i <= retomada.linhas:
                continue
            item_key = (r.get("item_key") or "").strip()
            if not item_key:
                rel.mais("linhas sem item_key")
                continue
            rel.mais("linhas lidas")
            yield item_key

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(), console=console) as barra:
        tarefa = barra.add_task("marcando sobreviventes", total=total,
                                completed=retomada.linhas)
        for lote in em_lotes(chaves(), LOTE):
            # Marcação e avanço da retomada no MESMO commit (docs/08_CONVENCOES.md §5.3):
            # separados, uma interrupção entre os dois faria a retomada pular itens não
            # marcados — que então nunca chegariam ao pareamento.
            with db.session() as s:
                rel.mais("marcados", repo.marcar_sobreviventes(s, lote))
                retomada.avancar(len(lote))
            barra.update(tarefa, completed=retomada.linhas)

    with db.session() as s:
        rel.mais("documentos recontados", repo.recontar_sobreviventes_por_documento(s))
        contagens = repo.contar(s)
    for chave, valor in contagens.items():
        rel.mais(f"{chave} no banco", valor)

    # Comparação contra as linhas EFETIVAMENTE lidas, nunca contra `estimar_linhas` — que é
    # um limite superior e faria este aviso disparar em todo arquivo com descrição multilinha.
    lidas = retomada.linhas
    marcados = contagens["item_sobrevivente"]
    if marcados < lidas:
        rel.aviso(f"{lidas - marcados} chaves do CSV não viraram sobreviventes no banco — "
                  f"são item_keys que não existem em `item` (linhas do CSV da etapa 4 cujo "
                  f"item não está em 2_itens_coletados.csv). Verifique antes de seguir.")
    return rel


def main() -> None:
    cabecalho("m09 — sobreviventes", paths.E4_SOBREVIVENTES,
              "item.sobrevivente, documento.n_itens_sobreviventes")
    console.print(f"  banco  : {db.database_url()}")
    migrar(reiniciar="--reiniciar" in sys.argv).imprimir()


if __name__ == "__main__":
    main()
