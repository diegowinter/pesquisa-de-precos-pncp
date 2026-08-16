"""
Semeia ARTIFICIALMENTE o watermark da etapa 2 a partir do acervo já coletado (seed do v2),
sem tocar no PNCP.

Contexto: o watermark de verdade usa `data_atualizacao_pncp` (a data por onde a busca do PNCP
vem ordenada). O v2 NÃO salvou esse campo — só a `data_publicacao_pncp` (coluna `data`). Como
substituto SEGURO usamos a publicação: vale sempre `publicação ≤ atualização`, então o carimbo
fica "atrás" (conservador). Consequência desejada: a próxima rodada `--atualizar` para de paginar
só num ponto mais antigo → ela VOLTA e coleta a lacuna v2→agora, nunca pula nada.

Para cada (termo, fonte) grava a MAIOR data de publicação entre os itens que aquele termo trouxe:
  - termo = conceito (esquema novo: 1 termo por linha), lido de `conceitos_origem`
  - fonte = tipo_doc (contrato | ata)
Termos que não coletaram nada ficam de fora → a etapa 2 os varre por completo (seguro).

Saída: data/checkpoints/2_watermark.csv  (termo, tipo_doc, data_max)

Uso:
  python ferramentas/semear_watermark_v2.py            # calcula e grava
  python ferramentas/semear_watermark_v2.py --dry-run  # só mostra, não grava
  python ferramentas/semear_watermark_v2.py --com-extra # inclui 2_conceitos_extra.csv (mais fiel)
  python ferramentas/semear_watermark_v2.py --forcar   # sobrescreve um watermark já existente
"""

import argparse
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

console = Console()

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
ITENS = DATA / "2_itens_coletados.csv"
EXTRA = DATA / "checkpoints" / "2_conceitos_extra.csv"
WATERMARK = DATA / "checkpoints" / "2_watermark.csv"

CHUNK = 200_000


def _acumular(wm: dict, termo: str, fonte: str, data: str) -> None:
    """Mantém em wm[(termo, fonte)] a MAIOR data (string ISO — comparação lexical serve)."""
    if not (termo and data):
        return
    chave = (termo, fonte)
    if data > wm.get(chave, ""):
        wm[chave] = data


def varrer_itens(wm: dict) -> tuple[int, dict[str, str]]:
    """Percorre 2_itens_coletados.csv em blocos, alimentando o watermark por (termo, fonte).

    Devolve (nº de linhas lidas, mapa item_key → 'fonte|data') — o mapa só é montado se
    `--com-extra`, para casar os conceitos extras (que não têm tipo_doc/data próprios).
    """
    if not (ITENS.exists() and ITENS.stat().st_size > 0):
        raise SystemExit(f"{ITENS} ausente/vazio. Nada a semear.")
    total = 0
    item_meta: dict[str, str] = {}
    cols = ["item_key", "tipo_doc", "conceitos_origem", "data"]
    for chunk in pd.read_csv(ITENS, usecols=cols, dtype=str, chunksize=CHUNK, encoding="utf-8"):
        chunk = chunk.fillna("")
        for ik, fonte, origem, data in zip(chunk["item_key"], chunk["tipo_doc"],
                                           chunk["conceitos_origem"], chunk["data"]):
            total += 1
            for termo in origem.split("|"):
                _acumular(wm, termo, fonte, data)
            if item_meta is not None and ik and data:
                item_meta[ik] = f"{fonte}|{data}"
        console.print(f"  [dim]{total:,} linhas lidas…[/]", end="\r")
    console.print(" " * 40, end="\r")
    return total, item_meta


def varrer_extras(wm: dict, item_meta: dict[str, str]) -> int:
    """Inclui os conceitos extras (item_key → conceito), casando fonte/data pelo item_key."""
    if not (EXTRA.exists() and EXTRA.stat().st_size > 0):
        console.print("[dim]  (sem 2_conceitos_extra.csv — pulando)[/]")
        return 0
    total = 0
    for chunk in pd.read_csv(EXTRA, usecols=["item_key", "conceito"], dtype=str,
                             chunksize=CHUNK, encoding="utf-8"):
        chunk = chunk.fillna("")
        for ik, termo in zip(chunk["item_key"], chunk["conceito"]):
            meta = item_meta.get(ik)
            if not meta:
                continue
            fonte, data = meta.split("|", 1)
            _acumular(wm, termo, fonte, data)
            total += 1
    return total


def gravar(wm: dict) -> None:
    """Grava o watermark de forma atômica, no mesmo formato que a etapa 2 lê."""
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    linhas = [{"termo": t, "tipo_doc": f, "data_max": d} for (t, f), d in sorted(wm.items())]
    tmp = WATERMARK.with_suffix(".csv.tmp")
    pd.DataFrame(linhas, columns=["termo", "tipo_doc", "data_max"]).to_csv(
        tmp, index=False, encoding="utf-8")
    os.replace(tmp, WATERMARK)


def resumo(wm: dict) -> None:
    """Imprime um panorama do watermark calculado (para conferência humana)."""
    if not wm:
        console.print("[yellow]Nenhum (termo, fonte) com data — watermark vazio.[/]")
        return
    datas = [d for d in wm.values() if d]
    por_fonte: dict[str, int] = {}
    for (_t, f) in wm:
        por_fonte[f] = por_fonte.get(f, 0) + 1
    console.print(f"[bold]{len(wm)}[/] marcadores (termo × fonte) · "
                  f"{', '.join(f'{f}: {n}' for f, n in sorted(por_fonte.items()))}")
    console.print(f"datas de publicação: [cyan]{min(datas)[:10]}[/] … [cyan]{max(datas)[:10]}[/]")
    console.print("[dim]amostra:[/]")
    for (t, f), d in sorted(wm.items())[:8]:
        console.print(f"  [dim]{t[:32]:<32} {f:<9} {d[:10]}[/]")


def main():
    ap = argparse.ArgumentParser(description="Semeia o watermark da etapa 2 a partir do acervo v2")
    ap.add_argument("--dry-run", action="store_true", help="Calcula e mostra, mas NÃO grava")
    ap.add_argument("--com-extra", action="store_true",
                    help="Inclui 2_conceitos_extra.csv (watermark mais fiel; usa mais memória)")
    ap.add_argument("--forcar", action="store_true", help="Sobrescreve um watermark já existente")
    args = ap.parse_args()

    if WATERMARK.exists() and not (args.forcar or args.dry_run):
        raise SystemExit(f"{WATERMARK} já existe. Use --forcar para sobrescrever "
                         f"(ou --dry-run para só conferir).")

    console.print("[bold]Semeando watermark artificial (data de publicação como marca segura)…[/]")
    wm: dict = {}
    total, item_meta = varrer_itens(wm)
    console.print(f"[green]Itens lidos:[/] {total:,}")
    if args.com_extra:
        n_extra = varrer_extras(wm, item_meta)
        console.print(f"[green]Associações extras aplicadas:[/] {n_extra:,}")

    resumo(wm)
    if args.dry_run:
        console.print("[yellow]--dry-run: nada gravado.[/]")
        return
    gravar(wm)
    console.print(f"[bold green]Watermark gravado[/] → {WATERMARK}")
    console.print("[dim]Próximo passo: python 2_coletar_pncp.py --atualizar "
                  "(vai parar em cada marca e coletar só a lacuna).[/]")


if __name__ == "__main__":
    main()
