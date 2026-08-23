"""
m12 — Pares: `6a_pares_candidatos.csv` + `6b_pares_rerankeados.csv` + `6c_pares_validados.csv`
→ `par` (uma tabela só, ADR-013).

**A 6b tem MAIS linhas que a 6a** (250.114 vs 220.781), porque acumula entre execuções
resumíveis enquanto a 6a é regravada inteira a cada rodada. Por isso o conjunto de `par_key` é
o da **6b**, com `LEFT JOIN` nas outras duas — usar a 6a como base descartaria 30 mil pares
rerankeados. Os pares sem correspondente na 6a ficam com `score_bm25`/`score_cosseno` nulos, e
quantos são aparece no relatório (docs/05_MIGRACAO.md §m12).

`codigo` nos CSVs não traz o `tipo`. Ele é resolvido pelo join com `catalogo_item`, sob a
premissa de que o código é único no catálogo filtrado — premissa que este script **valida e
aborta** se for falsa, em vez de assumir. Um código ambíguo migrado com o tipo errado apontaria
para outro item de catálogo, e o erro só apareceria como um preço estranho no export.

`final_decision` não é escrita linha a linha: é derivada (`confirmado` = decisão 'aceito' OU
veredito 'sim') e recomputada por SQL no fim, que é o único jeito de ela não divergir da regra.

Uso: python -m migracao.m12_pares
"""

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from sqlalchemy import text as sql

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import catalogo as repo_cat
from pesquisa_precos.db.repos import execution as repo_exec
from pesquisa_precos.db.repos import par as repo
from migracao._comum import Relatorio, cabecalho, console, existe, ler_csv, txt

LOTE = 20_000

# Vocabulário do CSV → enum do banco. 'sim'/'nao' já são os valores do enum `veredito_par`;
# o mapa existe para que qualquer outro texto vire NULL em vez de derrubar o COPY.
VEREDITO = {"sim": "sim", "nao": "nao", "não": "nao", "indeterminado": "indeterminado"}
DECISAO = {"aceito": "aceito", "ambiguo": "ambiguo", "ambíguo": "ambiguo",
           "rejeitado": "rejeitado"}


def _float(value) -> float | None:
    """Score é métrica adimensional, não dinheiro — `real` no banco, `float` aqui é correto."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def carregar_6a(rel: Relatorio) -> dict[str, tuple]:
    """`par_key → (codigo, item_key, categoria, bm25, cosseno, sobreviveu)`."""
    if not existe(paths.E6A_PARES):
        rel.aviso(f"{paths.E6A_PARES.name} ausente — todos os pares ficam sem score da 6a.")
        return {}
    mapa = {}
    for r in ler_csv(paths.E6A_PARES):
        pk = (r.get("par_key") or "").strip()
        if pk:
            mapa[pk] = ((r.get("codigo") or "").strip(), (r.get("item_key") or "").strip(),
                        (r.get("categoria") or "").strip(),
                        _float(r.get("score_bm25")), _float(r.get("score_cosseno")),
                        (r.get("sobreviveu") or "").strip().lower() == "true")
    rel.mais("6a carregados", len(mapa))
    return mapa


def carregar_6c(rel: Relatorio) -> dict[str, tuple]:
    """`par_key → (veredito, justificativa)`."""
    if not existe(paths.E6C_VALIDADOS):
        return {}
    mapa = {}
    for r in ler_csv(paths.E6C_VALIDADOS):
        pk = (r.get("par_key") or "").strip()
        if pk:
            mapa[pk] = (VEREDITO.get((r.get("mesmo_item") or "").strip().lower()),
                        txt(r.get("justificativa")))
    rel.mais("6c carregados", len(mapa))
    return mapa


def migrar() -> Relatorio:
    rel = Relatorio("m12 — pares")
    if not existe(paths.E6B_RERANKEADOS):
        raise SystemExit(f"{paths.E6B_RERANKEADOS} ausente. Rode a step 6b antes.")

    seis_a = carregar_6a(rel)
    seis_c = carregar_6c(rel)

    with db.session() as s:
        tipo_de, ambiguos = repo_cat.tipo_do_codigo(s)
        run_id = repo_exec.run_do_acervo_migrado(s)
    if ambiguos:
        raise SystemExit(
            f"ABORTADO: {len(ambiguos)} códigos existem nos DOIS tipos do catálogo "
            f"({', '.join(ambiguos[:5])}...). Os CSVs de par não guardam o tipo, então migrar "
            f"assim apontaria pares para o item de catálogo errado. Resolva a colisão no "
            f"catálogo antes de rodar o m12.")

    # Uma leitura da 6b para levantar as chaves, outra para gravar. O arquivo tem 16 MB;
    # duas passadas custam menos que carregar tudo e depois filtrar.
    console.print("  conferindo integridade referencial (item_key e codigo)…")
    par_keys = [(r.get("par_key") or "").strip()
                for r in ler_csv(paths.E6B_RERANKEADOS) if (r.get("par_key") or "").strip()]
    item_keys = {pk.split("::", 1)[1] for pk in par_keys if "::" in pk}
    with db.session() as s:
        existentes = set(s.scalars(
            sql("SELECT item_key FROM item WHERE item_key = ANY(:k)"),
            {"k": list(item_keys)}).all())
    rel.mais("item_keys distintos nos pares", len(item_keys))
    faltando = len(item_keys) - len(existentes)
    if faltando:
        rel.aviso(f"{faltando} item_keys dos pares NÃO existem em `item` — esses pares foram "
                  f"descartados (a FK os rejeitaria). São itens de execuções anteriores cujo "
                  f"registro não está em 2_itens_coletados.csv.")

    def linhas():
        for r in ler_csv(paths.E6B_RERANKEADOS):
            pk = (r.get("par_key") or "").strip()
            if not pk or "::" not in pk:
                rel.mais("pares sem par_key utilizável")
                continue
            codigo, item_key = pk.split("::", 1)
            tipo = tipo_de.get(codigo)
            if tipo is None:
                rel.mais("pares com código fora do catálogo (descartados)")
                continue
            if item_key not in existentes:
                rel.mais("pares com item inexistente (descartados)")
                continue

            base = seis_a.get(pk)
            if base is None:
                rel.mais("pares sem correspondente na 6a (scores nulos)")
                categoria, bm25, cosseno, sobreviveu = "", None, None, False
            else:
                _, _, categoria, bm25, cosseno, sobreviveu = base

            rel.mais("pares lidos")
            yield (pk, tipo, codigo, item_key, categoria,
                   bm25, cosseno, sobreviveu, run_id)

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(),
                  console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("gravando pares", total=len(par_keys))
        gravados = 0
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar_candidatos(conn, lote)
            conn.commit()
            gravados += len(lote)
            rel.mais("pares gravados", len(lote))
            barra.update(tarefa, completed=gravados)

    # 6b e 6c entram como UPDATE, sobre as linhas que acabaram de ser criadas.
    with db.session() as s:
        rerank = [((r.get("par_key") or "").strip(), _float(r.get("score_rerank")),
                   DECISAO.get((r.get("decisao") or "").strip().lower()))
                  for r in ler_csv(paths.E6B_RERANKEADOS)]
        for lote in em_lotes([x for x in rerank if x[0]], LOTE):
            rel.mais("scores de rerank aplicados", repo.gravar_rerank(s, lote))

        vereditos = [(pk, v, j, None) for pk, (v, j) in seis_c.items() if v]
        for lote in em_lotes(vereditos, LOTE):
            rel.mais("vereditos da 6c aplicados", repo.gravar_veredito(s, lote))

        rel.mais("final_decision recomputada", repo.recomputar_decisao_final(s))
        for key, value in repo.contar(s).items():
            rel.mais(f"{key} no banco", value)
    return rel


def main() -> None:
    cabecalho("m12 — pares", [paths.E6B_RERANKEADOS, paths.E6A_PARES, paths.E6C_VALIDADOS],
              "par")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
