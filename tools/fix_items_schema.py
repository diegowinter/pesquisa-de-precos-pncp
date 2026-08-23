"""
Reparo único de schema do data/2_itens_coletados.csv.

Motivo: o acervo migrado do v2 foi gravado com 17 colunas; o código atual do v3 grava 20
(add de preco_estimado, fornecedor, data_resultado). Uma coleta nova anexou linhas de 20 campos
sob um cabeçalho de 17 → o arquivo ficou misto e o pandas quebra ao reler
("Expected 17 fields ... saw 20").

Correção (sem perda): reescreve o CSV com o cabeçalho de 20 colunas (COLUNAS_ITENS). Linhas
antigas (17 campos) recebem os 3 campos novos VAZIOS no fim — a ordem casa, pois são exatamente
as 3 últimas colunas do schema novo. Linhas já com 20 campos passam intactas. Streaming +
escrita atômica (só troca o original ao final).

Uso: python tools/fix_items_schema.py [--dry-run]
"""

import argparse
import csv
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

csv.field_size_limit(10_000_000)  # descrições longas em campos citados

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from pesquisa_precos.config import paths  # noqa: E402
from pesquisa_precos.core.collection.collect_pncp import COLUNAS_ITENS  # noqa: E402

SAIDA = paths.E2_ITENS
N = len(COLUNAS_ITENS)  # 20


def main():
    ap = argparse.ArgumentParser(description="Repara o schema (17→20 col) do 2_itens_coletados.csv")
    ap.add_argument("--dry-run", action="store_true", help="Só conta, não reescreve")
    args = ap.parse_args()

    if not (SAIDA.exists() and SAIDA.stat().st_size > 0):
        raise SystemExit(f"{SAIDA} ausente/vazio.")

    tmp = SAIDA.with_suffix(".csv.fix")
    n17 = n20 = nout = 0
    with open(SAIDA, newline="", encoding="utf-8") as fin:
        r = csv.reader(fin)
        header = next(r)
        print(f"Cabeçalho atual: {len(header)} colunas · schema alvo: {N} colunas")
        fout = None if args.dry_run else open(tmp, "w", newline="", encoding="utf-8")
        try:
            w = csv.writer(fout) if fout else None
            if w:
                w.writerow(COLUNAS_ITENS)
            for row in r:
                if len(row) == 17:
                    row = row + ["", "", ""]
                    n17 += 1
                elif len(row) == N:
                    n20 += 1
                else:  # inesperado: pad/trunca defensivamente para N
                    nout += 1
                    row = (row + [""] * N)[:N]
                if w:
                    w.writerow(row)
        finally:
            if fout:
                fout.close()

    print(f"linhas 17-col (migradas, +3 vazias): {n17:,}")
    print(f"linhas 20-col (já no schema novo):   {n20:,}")
    if nout:
        print(f"linhas com contagem inesperada (ajustadas p/ {N}): {nout:,}")
    if args.dry_run:
        print("--dry-run: nada reescrito.")
        return
    os.replace(tmp, SAIDA)
    print(f"OK — {SAIDA.name} reescrito com {N} colunas.")


if __name__ == "__main__":
    main()
