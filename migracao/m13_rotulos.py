"""
m13 — Rótulos: `6_rotulos_acumulados.csv` → `rotulo`.

250.085 linhas acumuladas ao longo de todas as execuções de 6b/6c. É a base de calibração de
threshold e o insumo de qualquer fine-tune futuro do reranker — tabela append-only que **nunca
se trunca** (docs/02_SCHEMA.md §7). Migrar é transporte puro: nada é derivado, nada é filtrado.

`UNIQUE (par_key, origem)` faz o dedup: o CSV traz o mesmo par mais de uma vez quando ele foi
reavaliado, e o `ON CONFLICT DO NOTHING` mantém a primeira ocorrência. A coluna `timestamp` do
CSV não tem destino no schema — `criado_em` recebe o `now()` da migração. Isso perde a data
original do rótulo; é uma perda real e pequena (a ordem relativa dentro do arquivo é
preservada) que fica registrada aqui para quem for calibrar por época.

Uso: python -m migracao.m13_rotulos [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import execution as repo_exec
from pesquisa_precos.db.repos import par as repo
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    existe,
    ler_csv,
    txt,
)

LOTE = 20_000


def migrar(reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m13 — rótulos")
    if not existe(paths.E6_ROTULOS):
        raise SystemExit(f"{paths.E6_ROTULOS} ausente. Rode as etapas 6b/6c antes.")

    retomada = Retomada.carregar("m13_rotulos")
    if reiniciar:
        retomada.zerar()

    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E6_ROTULOS)
    rel.mais("registros no CSV (estimado)", total)

    with db.session() as s:
        run_id = repo_exec.run_do_acervo_migrado(s)

    def linhas():
        for i, r in enumerate(ler_csv(paths.E6_ROTULOS), 1):
            if i <= retomada.linhas:
                continue
            pk = (r.get("par_key") or "").strip()
            origem = (r.get("origem") or "").strip()
            if not (pk and origem):
                rel.mais("linhas sem par_key/origem")
                continue
            score = (r.get("score_rerank") or "").strip()
            rel.mais("linhas lidas")
            yield (pk, r.get("texto_catalogo") or "", r.get("texto_item") or "",
                   float(score) if score else None,
                   (r.get("decisao_final") or "").strip(), origem,
                   txt(r.get("modelo")), run_id)

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(),
                  console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("gravando rótulos", total=total, completed=retomada.linhas)
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar_rotulos(conn, lote)
            conn.commit()
            retomada.avancar(len(lote))
            rel.mais("enviados", len(lote))
            barra.update(tarefa, completed=retomada.linhas)

    with db.session() as s:
        n = repo.contar(s)["rotulo"]
    rel.mais("rotulo no banco", n)
    lidas = retomada.linhas  # linhas reais; `total` é só o limite superior da barra
    if n < lidas:
        rel.aviso(f"{lidas - n} linhas do CSV colapsaram por UNIQUE (par_key, origem) — "
                  f"são reavaliações do mesmo par pela mesma origem. Esperado.")
    return rel


def main() -> None:
    cabecalho("m13 — rótulos", paths.E6_ROTULOS, "rotulo")
    console.print(f"  banco  : {db.database_url()}")
    migrar(reiniciar="--reiniciar" in sys.argv).imprimir()


if __name__ == "__main__":
    main()
