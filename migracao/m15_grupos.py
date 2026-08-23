"""
m15 — Grupos: `7_itens_agrupados.csv` → `grupo_item`.

O CSV da step 7 já é o resultado do corte: só os confirmados não-sinalizados dos códigos que
fecharam, ordenados por preço unitário crescente. O que ele **não** traz é a coluna `posicao` —
o ranking está implícito na ordem das linhas.

`posicao` é reconstruída aqui, contando a ordem de aparição dentro de cada código. Isso funciona
porque a step 7 ordena por preço com `mergesort` (estável) antes de gravar: a ordem do arquivo
É o ranking. Recalcular por preço aqui daria empates resolvidos de outro jeito e produziria um
ranking ligeiramente diferente do que já foi entregue ao cliente.

`run_id` é o run sintético do m03 (`grupo_item.run_id` é NOT NULL — ver a justificativa lá).

Uso: python -m migracao.m15_grupos
"""

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import catalogo as repo_cat
from pesquisa_precos.db.repos import execution as repo_exec
from pesquisa_precos.db.repos import grupo as repo
from migracao._comum import (
    Relatorio,
    cabecalho,
    console,
    estimar_linhas,
    dec,
    existe,
    ler_csv,
)

LOTE = 20_000


def migrar() -> Relatorio:
    rel = Relatorio("m15 — grupos")
    if not existe(paths.E7_AGRUPADOS):
        raise SystemExit(f"{paths.E7_AGRUPADOS} ausente. Rode a step 7 antes.")

    with db.session() as s:
        tipo_de, ambiguos = repo_cat.tipo_do_codigo(s)
        run_id = repo_exec.run_do_acervo_migrado(s)
        if run_id is None:
            raise SystemExit("run do acervo migrado não existe. Rode `python -m "
                             "migracao.m03_run_historico` antes.")
    if ambiguos:
        raise SystemExit(
            f"ABORTADO: códigos ambíguos no catálogo ({', '.join(ambiguos[:5])}...). "
            f"Mesma razão do m12: o CSV de grupos não guarda o tipo.")

    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E7_AGRUPADOS)
    rel.mais("registros no CSV (estimado)", total)

    posicao_por_codigo: dict[str, int] = {}
    itens_ja_vistos: set[tuple[str, str]] = set()

    def linhas():
        for r in ler_csv(paths.E7_AGRUPADOS):
            codigo = (r.get("codigo") or "").strip()
            item_key = (r.get("item_key") or "").strip()
            par_key = (r.get("par_key") or "").strip()
            if not (codigo and item_key and par_key):
                rel.mais("linhas sem key")
                continue
            tipo = tipo_de.get(codigo)
            if tipo is None:
                rel.mais("linhas com código fora do catálogo (descartadas)")
                continue
            # A UNIQUE do destino é (run_id, tipo, codigo, item_key): o mesmo item aparecer
            # duas vezes sob o mesmo código faria a segunda linha sumir no ON CONFLICT sem
            # ninguém notar. Contamos aqui para que apareça.
            if (codigo, item_key) in itens_ja_vistos:
                rel.mais("linhas duplicadas (codigo, item_key)")
                continue
            itens_ja_vistos.add((codigo, item_key))

            posicao = posicao_por_codigo.get(codigo, 0) + 1
            posicao_por_codigo[codigo] = posicao
            flag = (r.get("flag_preco") or "").strip().lower() == "true"
            rel.mais("linhas lidas")
            yield (tipo, codigo, item_key, par_key, posicao,
                   dec(r.get("preco_unitario")), flag, None, run_id)

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("gravando grupos", total=total)
        enviados = 0
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar(conn, lote)
            conn.commit()
            enviados += len(lote)
            rel.mais("gravados", len(lote))
            barra.update(tarefa, completed=enviados)

    rel.mais("códigos distintos", len(posicao_por_codigo))
    with db.session() as s:
        for key, value in repo.contar(s).items():
            rel.mais(f"{key} no banco", value)
    return rel


def main() -> None:
    cabecalho("m15 — grupos", paths.E7_AGRUPADOS, "grupo_item")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
