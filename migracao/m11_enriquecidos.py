"""
m11 — Itens enriquecidos: `5_itens_enriquecidos.csv` + `5_itens_destino.csv` →
`item_enriquecido`, `documento_extracao`, `documento.estado`.

Os dois CSVs se juntam por `item_key`. O de destino (22 MB, 302.514 linhas) é carregado em
memória porque é pequeno e é consultado a cada linha do outro; o de enriquecidos (94 MB) é
lido em streaming.

Mapeamentos que valem registrar:

  `enriquecimento` → `status`    1:1 com o enum `status_enriquecimento` (conferido no acervo:
                                 os 7 valores presentes existem todos no enum).
  `doc_status`     → `documento.estado`   ok→extraido, suspeito→suspeito, ilegivel→ilegivel.
  `estrategia`     = 'window' para TODO o acervo — foi o único caminho usado na v2/v3. Marcar
                     assim é o que vai permitir, na Fase 8, comparar a `completa` contra uma
                     linha de base identificada.
  `paginas_ocr`    → `documento_extracao.n_paginas_ocr`, agregado por documento (máximo: o
                     valor se repete em todos os itens do mesmo documento).

`cost_usd`/`tokens` de `documento_extracao` ficam ZERADOS e `model`/`provider` NULL: a v2/v3
não mediu nada disso. Preencher com estimativa contaminaria a série histórica de custo que a
Fase 3 vai construir — o dado ausente precisa continuar ausente.

`divergencia_preco` é SINAL, não erro (docs/08_CONVENCOES.md §5.9): a API traz o estimado, o
PDF traz o homologado. Migra como está, sem filtro.

Uso: python -m migracao.m11_enriquecidos [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import documento as repo_doc
from pesquisa_precos.db.repos import execution as repo_exec
from pesquisa_precos.db.repos import extraction as repo
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    dec,
    existe,
    inteiro,
    ler_csv,
    txt,
)

LOTE = 10_000

ESTADO_DO_DOC_STATUS = {"ok": "extraido", "suspeito": "suspeito", "ilegivel": "ilegivel"}

# Sem linha em 5_itens_destino.csv não há doc_status nem destino. Acontece se os dois CSVs
# ficaram fora de sincronia entre execuções; o item vai para 'revisar' (nunca 'manter', que o
# mandaria ao pareamento sem confirmação) e o caso é contado.
DESTINO_PADRAO = ("suspeito", "revisar")


def carregar_destino(rel: Relatorio) -> dict[str, tuple[str, str]]:
    """`item_key → (doc_status, destino)`."""
    if not existe(paths.E5_DESTINO):
        rel.aviso(f"{paths.E5_DESTINO.name} ausente — todos os itens caem no padrão "
                  f"{DESTINO_PADRAO}, o que os tira do pareamento. Confira antes de seguir.")
        return {}
    mapa = {}
    for r in ler_csv(paths.E5_DESTINO):
        item_key = (r.get("item_key") or "").strip()
        if item_key:
            mapa[item_key] = ((r.get("doc_status") or "").strip(),
                              (r.get("destino") or "").strip())
    rel.mais("destinos carregados", len(mapa))
    return mapa


def migrar(reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m11 — itens enriquecidos")
    if not existe(paths.E5_ENRIQUECIDOS):
        raise SystemExit(f"{paths.E5_ENRIQUECIDOS} ausente. Rode a step 5b antes.")

    retomada = Retomada.carregar("m11_enriquecidos")
    if reiniciar:
        retomada.zerar()

    destinos = carregar_destino(rel)
    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E5_ENRIQUECIDOS)
    rel.mais("registros no CSV (estimado)", total)

    with db.session() as s:
        run_id = repo_exec.run_do_acervo_migrado(s)

    # Acumuladores por documento — 68 mil chaves, cabe em memória.
    ocr_por_doc: dict[str, int] = {}
    estado_por_doc: dict[str, str] = {}

    def linhas():
        for i, r in enumerate(ler_csv(paths.E5_ENRIQUECIDOS), 1):
            if i <= retomada.linhas:
                continue
            item_key = (r.get("item_key") or "").strip()
            if not item_key:
                rel.mais("linhas sem item_key")
                continue
            doc_status, destino = destinos.get(item_key, DESTINO_PADRAO)
            if item_key not in destinos:
                rel.mais("itens sem linha em 5_itens_destino.csv")
            doc_status = doc_status if doc_status in ESTADO_DO_DOC_STATUS else "suspeito"
            destino = destino if destino in ("manter", "revisar", "descartar") else "revisar"

            nc = item_key.split("::", 1)[0]
            paginas_ocr = inteiro(r.get("paginas_ocr")) or 0
            ocr_por_doc[nc] = max(ocr_por_doc.get(nc, 0), paginas_ocr)
            estado_por_doc[nc] = ESTADO_DO_DOC_STATUS[doc_status]

            rel.mais("linhas lidas")
            yield (item_key, r.get("descricao_final") or "",
                   (r.get("fonte_descricao") or "api").strip(),
                   dec(r.get("preco_api")), dec(r.get("preco_pdf")),
                   dec(r.get("divergencia_preco")),
                   txt(r.get("fornecedor")), dec(r.get("quantidade_pdf")),
                   (r.get("enriquecimento") or "erro").strip(),
                   destino, "window", ESTADO_DO_DOC_STATUS[doc_status], run_id)

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(),
                  console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("gravando enriquecidos", total=total,
                                completed=retomada.linhas)
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar_enriquecidos(conn, lote)
            conn.commit()
            retomada.avancar(len(lote))
            rel.mais("gravados", len(lote))
            barra.update(tarefa, completed=retomada.linhas)

    # `documento_extracao`: uma linha por documento, estratégia 'window', custo NÃO medido.
    with db.raw_connection() as conn:
        extracoes = [
            (nc, "window", None, None, ocr_por_doc.get(nc), 0, 0, 0, None, None, None, run_id)
            for nc in estado_por_doc
        ]
        for lote in em_lotes(extracoes, LOTE):
            rel.mais("documento_extracao gravados", repo.gravar_extracoes(conn, lote))

    with db.session() as s:
        rel.mais("documentos com estado atualizado",
                 repo_doc.atualizar_estado(s, list(estado_por_doc.items())))
        for key, value in repo.contar(s).items():
            rel.mais(f"{key} no banco", value)
    return rel


def main() -> None:
    cabecalho("m11 — itens enriquecidos", [paths.E5_ENRIQUECIDOS, paths.E5_DESTINO],
              "item_enriquecido, documento_extracao, documento.estado")
    console.print(f"  banco  : {db.database_url()}")
    migrar(reiniciar="--reiniciar" in sys.argv).imprimir()


if __name__ == "__main__":
    main()
