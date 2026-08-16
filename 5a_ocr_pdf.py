"""
Etapa 5a — Parse + OCR dos PDFs dos sobreviventes (paralelo, resumível por documento).

Isola o gargalo de OCR da extração LLM (5b). Para cada documento (pasta_arquivos) dos itens
sobreviventes: abre os PDFs com PyMuPDF, extrai o texto nativo por página; página escaneada
(< 100 chars nativos) é rasterizada e enviada ao servidor de OCR remoto (OCR_BASE_URL — o
PaddleOCR/GPU). O texto de cada página é gravado em data/5_pdf_texto.csv (append crash-safe,
fsync por linha), resumível por doc_key: relançar pula os documentos já processados.

A concorrência (--concurrency) processa vários DOCUMENTOS ao mesmo tempo; cada worker faz um
documento por vez (páginas em sequência). Case este valor com o --workers do servidor de OCR.

Entrada: data/4_itens_sobreviventes.csv (coluna pasta_arquivos).
Saída: data/5_pdf_texto.csv (doc_key, arquivo, pagina, fonte, texto). Chave de resumo: doc_key.
Uso: python 5a_ocr_pdf.py [--concurrency 3] [--pular-ocr] [--limite-docs N]
"""

import argparse
import glob
import os
import sys
from pathlib import Path

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

from scripts import ocr_pdf
from scripts.config import carregar_config, exigir
from scripts.io_seguro import EscritorSeguro, ler_chaves_concluidas
from scripts.paralelo import executar_paralelo

console = Console()

DATA = Path(__file__).resolve().parent / "data"
SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"
SAIDA = DATA / "5_pdf_texto.csv"

COLS = ["doc_key", "arquivo", "pagina", "fonte", "texto"]


def parse_documento(pasta: str, pular_ocr: bool, cfg: dict) -> list[dict]:
    """Devolve [{doc_key, arquivo, pagina, fonte, texto}] para todos os PDFs da pasta.
    Página escaneada vai ao OCR remoto (uma imagem por chamada — nunca o doc inteiro)."""
    registros = []
    for pdf in sorted(glob.glob(os.path.join(pasta, "*.pdf")) + glob.glob(os.path.join(pasta, "*.PDF"))):
        try:
            paginas = ocr_pdf.extrair_paginas(pdf)
        except Exception:  # noqa: BLE001
            continue
        for pg in paginas:
            fonte, texto = "nativo", pg["texto"]
            if ocr_pdf.pagina_escaneada(pg["densidade"]) and not pular_ocr:
                try:
                    png = ocr_pdf.rasterizar(pdf, pg["_page_index"])
                    ocr_txt = ocr_pdf.ocr_pagina(png, cfg["ocr_base_url"], cfg["ocr_model"], cfg["ocr_api_key"])
                    if ocr_txt:
                        fonte, texto = "ocr", ocr_txt
                except Exception:  # noqa: BLE001
                    pass
            registros.append({"doc_key": pasta, "arquivo": os.path.basename(pdf),
                              "pagina": pg["pagina"], "fonte": fonte, "texto": texto})
    return registros


def main():
    ap = argparse.ArgumentParser(description="Etapa 5a — parse + OCR dos PDFs")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="documentos processados em paralelo (case com --workers do servidor OCR)")
    ap.add_argument("--pular-ocr", action="store_true", help="Só PDFs nativos (não chama o OCR)")
    ap.add_argument("--limite-docs", type=int, default=None)
    args = ap.parse_args()

    cfg = carregar_config()
    if not args.pular_ocr:
        msg = exigir(cfg, "ocr")
        if msg:
            raise SystemExit(msg)
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    docs = [p for p in dict.fromkeys(sob["pasta_arquivos"]) if p and os.path.isdir(p)]
    feitos = ler_chaves_concluidas(str(SAIDA), "doc_key")
    pend = [d for d in docs if d not in feitos]
    if args.limite_docs:
        pend = pend[: args.limite_docs]
    if not pend:
        console.print("[5a] Nada a fazer (todos os documentos já processados).")
        return

    console.print(f"[bold][5a] {len(docs)} documentos, já feitos: {len(feitos)}, "
                  f"pendentes: {len(pend)}[/] — concorrência: {args.concurrency}"
                  f"{' (SEM OCR)' if args.pular_ocr else ''}")

    n_ocr = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        def fn(pasta):
            return parse_documento(pasta, args.pular_ocr, cfg)

        def ok(_pasta, regs):
            for r in regs:
                if r["fonte"] == "ocr":
                    n_ocr[0] += 1
                w.escrever(r)

        def err(pasta, exc):
            console.print(f"[yellow][5a] erro em {os.path.basename(pasta)}: {str(exc)[:120]}[/]")

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        )
        with progress:
            tarefa = progress.add_task("ocr/parse", total=len(pend))
            executar_paralelo(pend, fn, concurrency=args.concurrency, on_result=ok, on_error=err,
                              on_progress=lambda f, t: progress.update(tarefa, completed=f))
    console.print(f"[bold green][5a] Concluído.[/] {n_ocr[0]} páginas via OCR. → {SAIDA}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow][5a] Interrompido — progresso salvo (resumível por documento).[/]")
        sys.exit(130)
