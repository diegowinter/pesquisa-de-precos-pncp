"""
m10 — Texto de PDF: `5_pdf_texto.csv` (2,6 GB) → `documento_pagina`.

O arquivo é o maior do acervo: 888.656 linhas, cada uma com uma página inteira de texto. Duas
consequências práticas: lote de `COPY` de 1.000 (não 5.000), e `csv.field_size_limit` elevado —
sem ele o módulo `csv` aborta em algum campo grande no meio do arquivo.

O problema de chave: `doc_key` no CSV é o **caminho absoluto** da pasta do PDF, não o número de
controle. O mapa `caminho → numero_controle_pncp` vem de `2_itens_coletados.csv`
(`pasta_arquivos` → `numeroControlePNCP`), reconstruído aqui em streaming. Documentos cujo
`doc_key` não mapear são **contados e reportados**, nunca silenciados
(docs/05_MIGRACAO.md §m10) — cada um é um documento cujo texto extraído se perde na migração,
e isso precisa ser uma decisão consciente, não um número que ninguém viu.

O caminho é normalizado antes de comparar (barras e caixa): ~90% do acervo aponta para a pasta
do repositório v2 em Windows, e os separadores aparecem misturados nos dois arquivos.

`documento.n_paginas` é recomputado do próprio banco no fim. Ele é a rede de segurança da
política de retenção: sem saber quantas páginas o documento tinha, apagar o texto vira aposta.

Uso: python -m migracao.m10_texto_pdf [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from sqlalchemy import text as sql

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes, texto_para_pg
from pesquisa_precos.db.repos import extraction as repo
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    existe,
    inteiro,
    ler_csv,
)

LOTE = repo.LOTE_PAGINA


def normalizar_caminho(caminho: str) -> str:
    """Barras unificadas, sem barra final, em minúsculas. Comparação de caminho no Windows."""
    return (caminho or "").strip().replace("\\", "/").rstrip("/").lower()


def mapa_pasta_para_controle(rel: Relatorio) -> dict[str, str]:
    """`pasta_arquivos normalizada → numeroControlePNCP`, lido em streaming do CSV da etapa 2.

    Não vem do banco de propósito: `pasta_arquivos` NÃO é migrada (ADR-012). Este mapa é a
    última vez que o caminho absoluto é usado no projeto.
    """
    mapa: dict[str, str] = {}
    console.print("  reconstruindo o mapa pasta→numeroControlePNCP…")
    for r in ler_csv(paths.E2_ITENS):
        pasta = normalizar_caminho(r.get("pasta_arquivos", ""))
        nc = (r.get("numeroControlePNCP") or "").strip()
        if pasta and nc:
            mapa.setdefault(pasta, nc)
    rel.mais("pastas mapeadas", len(mapa))
    return mapa


def migrar(reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m10 — texto de PDF")
    if not existe(paths.E5_PDF_TEXTO):
        raise SystemExit(f"{paths.E5_PDF_TEXTO} ausente. Rode a etapa 5a antes.")
    if not existe(paths.E2_ITENS):
        raise SystemExit(f"{paths.E2_ITENS} ausente — é dele que sai o mapa de doc_key.")

    retomada = Retomada.carregar("m10_texto_pdf")
    if reiniciar:
        retomada.zerar()

    mapa = mapa_pasta_para_controle(rel)
    console.print("  contando linhas do CSV de texto…")
    total = estimar_linhas(paths.E5_PDF_TEXTO)
    rel.mais("registros no CSV (estimado)", total)

    nao_mapeados: set[str] = set()

    def linhas():
        for i, r in enumerate(ler_csv(paths.E5_PDF_TEXTO), 1):
            if i <= retomada.linhas:
                continue
            doc_key = r.get("doc_key", "")
            nc = mapa.get(normalizar_caminho(doc_key))
            if not nc:
                nao_mapeados.add(doc_key)
                rel.mais("páginas sem documento correspondente")
                continue
            pagina = inteiro(r.get("pagina"))
            arquivo = (r.get("arquivo") or "").strip()
            if pagina is None or not arquivo:
                rel.mais("páginas sem arquivo/número")
                continue
            rel.mais("páginas lidas")
            bruto = r.get("texto") or ""
            texto = texto_para_pg(bruto)
            if texto is not bruto:
                rel.mais("páginas com byte NUL removido")
            yield (nc, arquivo, pagina, (r.get("fonte") or "nativo").strip(), texto)

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(),
                  console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("gravando páginas", total=total, completed=retomada.linhas)
        # `em_lotes` recebe o gerador, então as linhas descartadas (sem documento) também
        # precisam avançar a retomada — senão uma reexecução as reprocessaria. Por isso o
        # avanço é pelo contador de linhas LIDAS do CSV, não pelo tamanho do lote gravado.
        lidas_antes = retomada.linhas
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar_paginas(conn, lote)
            conn.commit()
            lidas = (lidas_antes + rel.contadores.get("páginas lidas", 0)
                     + rel.contadores.get("páginas sem documento correspondente", 0)
                     + rel.contadores.get("páginas sem arquivo/número", 0))
            retomada.linhas = lidas
            retomada.salvar()
            rel.mais("páginas gravadas", len(lote))
            barra.update(tarefa, completed=retomada.linhas)

    if nao_mapeados:
        rel.mais("doc_keys distintos não mapeados", len(nao_mapeados))
        rel.aviso(f"{len(nao_mapeados)} doc_keys não casaram com nenhum documento — o texto "
                  f"extraído deles NÃO foi migrado. Exemplo: {sorted(nao_mapeados)[0][:110]}")

    with db.session() as s:
        s.execute(sql("""
            UPDATE documento d
               SET n_paginas = c.n, atualizado_em = now()
              FROM (SELECT numero_controle_pncp, count(DISTINCT pagina) AS n
                      FROM documento_pagina GROUP BY numero_controle_pncp) c
             WHERE c.numero_controle_pncp = d.numero_controle_pncp
               AND d.n_paginas IS DISTINCT FROM c.n
        """))
        contagens = repo.contar(s)
        for chave, valor in contagens.items():
            rel.mais(f"{chave} no banco", valor)

    # Divergência esperada e MEDIDA, não suposta: `5_pdf_texto.csv` é append-only e a etapa 5a
    # rodou mais de uma vez sobre os mesmos documentos, então a mesma (documento, arquivo,
    # página) aparece repetida — com texto idêntico. A PK do destino dedupa. Aferido na
    # amostra do acervo: fator ~2. Isso explica a diferença contra as 888.656 linhas do CSV
    # citadas em 02_SCHEMA.md §1, que contam LINHAS, não páginas distintas.
    lidas = rel.contadores.get("páginas lidas", 0)
    no_banco = contagens["documento_pagina"]
    if lidas and no_banco < lidas:
        rel.aviso(f"{lidas - no_banco} linhas colapsaram na PK (documento, arquivo, página) — "
                  f"são reextrações da mesma página, com texto igual. Esperado; o CSV é "
                  f"append-only.")

    # VACUUM não roda dentro de transação — daí a conexão em autocommit (§m10).
    console.print("  VACUUM ANALYZE documento_pagina… (pode demorar)")
    with db.raw_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE documento_pagina")
    return rel


def main() -> None:
    cabecalho("m10 — texto de PDF", [paths.E5_PDF_TEXTO, paths.E2_ITENS], "documento_pagina")
    console.print(f"  banco  : {db.database_url()}")
    migrar(reiniciar="--reiniciar" in sys.argv).imprimir()


if __name__ == "__main__":
    main()
