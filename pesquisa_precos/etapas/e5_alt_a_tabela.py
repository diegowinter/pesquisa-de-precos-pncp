"""
Etapa 5_alt_a — extração da TABELA de itens direto do PDF via modelo de VISÃO (paralelo,
resumível por documento). Alternativa ao 5a(OCR)+janela: em vez de OCR + texto corrido,
rasteriza cada página e manda a IMAGEM a um modelo de visão, que devolve a tabela de itens
"as it is" (layouts variam por documento — transcreve, não normaliza).

Para cada documento (pasta_arquivos) dos itens sobreviventes: abre os PDFs com PyMuPDF,
rasteriza cada página a 200 DPI e envia UMA imagem por chamada ao modelo de visão. As linhas
extraídas vão para data/5alt_tabela.csv (append crash-safe), resumível por doc_key — um doc
sem nenhuma tabela grava uma linha-sentinela (__SEM_ITENS__) para não ser reprocessado.

Entrada: data/4_itens_sobreviventes.csv (coluna pasta_arquivos).
Saída: data/5alt_tabela.csv (doc_key, arquivo, pagina, linha, numero_item, descricao,
       unidade, quantidade, preco_unitario, preco_total, fornecedor). Chave de resumo: doc_key.
Uso: python -m pesquisa_precos.etapas.e5_alt_a_tabela --provedor openrouter [--concurrency 6]
     [--modelo <vision-model>] [--max-paginas 15] [--limite-docs N]
Config: OPENAI_MODEL_VISION no .env (ou --modelo). Precisa ser um modelo com visão
        (ex.: qwen/qwen2.5-vl-72b-instruct, google/gemini-2.5-flash, etc.).
"""

import argparse
import glob
import os
import sys
import threading

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from pesquisa_precos.config import paths
from pesquisa_precos.providers import ocr_pdf
from pesquisa_precos.config.settings import carregar_config, exigir, resolver_provedor
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.providers.llm_curador import Curador
from pesquisa_precos.core.paralelo import executar_paralelo

console = Console()

SOBREVIVENTES = paths.E4_SOBREVIVENTES
SAIDA = paths.E5ALT_TABELA

COLS = ["doc_key", "arquivo", "pagina", "linha", "numero_item", "descricao",
        "unidade", "quantidade", "preco_unitario", "preco_total", "fornecedor"]

SENTINELA_VAZIO = "__SEM_ITENS__"

# Um Curador (visão) por thread — compartilhar cliente serializa as chamadas (ver etapa 3).
_tls = threading.local()


def _curador(cfg: dict, provedor: str, modelo: str) -> Curador:
    if not getattr(_tls, "c", None):
        p = resolver_provedor(cfg, provedor)
        _tls.c = Curador(model=modelo, base_url=p["base_url"], api_key=p["api_key"],
                         max_retries=6, temperature=0.0)
    return _tls.c


def extrair_doc(pasta: str, cfg: dict, provedor: str, modelo: str, max_paginas: int) -> list[dict]:
    """Rasteriza cada página dos PDFs da pasta e extrai a tabela via visão. Uma imagem/chamada."""
    cur = _curador(cfg, provedor, modelo)
    registros: list[dict] = []
    for pdf in sorted(glob.glob(os.path.join(pasta, "*.pdf")) + glob.glob(os.path.join(pasta, "*.PDF"))):
        try:
            paginas = ocr_pdf.extrair_paginas(pdf)
        except Exception:  # noqa: BLE001
            continue
        if max_paginas:
            paginas = paginas[:max_paginas]
        for pg in paginas:
            try:
                png = ocr_pdf.rasterizar(pdf, pg["_page_index"])
                linhas = cur.extrair_tabela_pdf(png)
            except Exception:  # noqa: BLE001
                continue
            for i, l in enumerate(linhas):
                registros.append({
                    "doc_key": pasta, "arquivo": os.path.basename(pdf),
                    "pagina": pg["pagina"], "linha": i, **l,
                })
    return registros


def main():
    ap = argparse.ArgumentParser(description="Etapa 5_alt_a — extrai tabela de itens do PDF (visão)")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="openrouter")
    ap.add_argument("--concurrency", type=int, default=6, help="documentos em paralelo")
    ap.add_argument("--modelo", default=None, help="modelo de visão (sobrepõe OPENAI_MODEL_VISION)")
    ap.add_argument("--max-paginas", type=int, default=15, help="teto de páginas por documento")
    ap.add_argument("--limite-docs", type=int, default=None)
    args = ap.parse_args()

    cfg = carregar_config()
    msg = exigir(cfg, args.provedor)
    if msg:
        raise SystemExit(msg)
    modelo = args.modelo or cfg["model_vision"]
    if not modelo:
        raise SystemExit("Modelo de visão ausente. Defina OPENAI_MODEL_VISION no .env ou use --modelo.")
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    docs = [p for p in dict.fromkeys(sob["pasta_arquivos"]) if p and os.path.isdir(p)]
    feitos = ler_chaves_concluidas(str(SAIDA), "doc_key")
    pend = [d for d in docs if d not in feitos]
    if args.limite_docs:
        pend = pend[: args.limite_docs]
    if not pend:
        console.print("[5_alt_a] Nada a fazer (todos os documentos já processados).")
        return

    console.print(f"[bold][5_alt_a] {len(docs)} documentos, já feitos: {len(feitos)}, "
                  f"pendentes: {len(pend)}[/] — modelo: {modelo}, concorrência: {args.concurrency}")

    n_linhas = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        def fn(pasta):
            return extrair_doc(pasta, cfg, args.provedor, modelo, args.max_paginas)

        def ok(pasta, regs):
            if not regs:  # doc sem tabela: sentinela para não reprocessar no resume
                w.escrever({"doc_key": pasta, "descricao": SENTINELA_VAZIO})
                return
            for r in regs:
                w.escrever(r)
                n_linhas[0] += 1

        def err(pasta, exc):
            console.print(f"[yellow][5_alt_a] erro em {os.path.basename(pasta)}: {str(exc)[:120]}[/]")

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        )
        with progress:
            tarefa = progress.add_task("extraindo tabelas", total=len(pend))
            executar_paralelo(pend, fn, concurrency=args.concurrency, on_result=ok, on_error=err,
                              on_progress=lambda f, t: progress.update(tarefa, completed=f))
    console.print(f"[bold green][5_alt_a] Concluído.[/] {n_linhas[0]} linhas de tabela extraídas. → {SAIDA}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow][5_alt_a] Interrompido — progresso salvo (resumível por documento).[/]")
        sys.exit(130)
