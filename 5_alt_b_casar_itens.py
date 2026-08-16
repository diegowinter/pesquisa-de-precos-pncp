"""
Etapa 5_alt_b — casa cada item da API do PNCP contra a TABELA LIMPA extraída em 5_alt_a
(paralelo, resumível por item). Alternativa ao 5b: em vez de dar o texto corrido do doc ao
modelo e pedir que ache o item (alto risco de alucinação/eco da descrição), aqui a entrada
já é a tabela estruturada do documento — o modelo só escolhe a LINHA certa (ou nenhuma).

Para cada item sobrevivente: pega as linhas da tabela do seu documento (doc_key =
pasta_arquivos) e pede ao LLM a linha correspondente. Grava preço do PDF, divergência vs. o
preço estimado da API (com banda de sanidade p/ misparse) e um status. Consolida o destino
(manter/revisar/descartar) por item_key. Resumível por item_key.

Entrada: data/4_itens_sobreviventes.csv + data/5alt_tabela.csv (de 5_alt_a).
Saída: data/5alt_itens_enriquecidos.csv (item_key, descricao_final, fonte_descricao,
       preco_api, preco_pdf, divergencia_preco, fornecedor, status) e
       data/5alt_itens_destino.csv (item_key, descricao_final, preco_api, preco_pdf,
       divergencia_preco, status, destino).
Uso: python 5_alt_b_casar_itens.py --provedor openrouter [--concurrency 8]
"""

import argparse
import re
import sys
import threading
from collections import defaultdict
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

from scripts.config import carregar_config, exigir
from scripts.io_seguro import EscritorSeguro, ler_chaves_concluidas, ler_csv, escrever_csv
from scripts.llm_curador import Curador
from scripts.paralelo import executar_paralelo

console = Console()

DATA = Path(__file__).resolve().parent / "data"
SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"
TABELA = DATA / "5alt_tabela.csv"
SAIDA = DATA / "5alt_itens_enriquecidos.csv"
DESTINO = DATA / "5alt_itens_destino.csv"

SENTINELA_VAZIO = "__SEM_ITENS__"
PRECO_SUSPEITO_MIN = 0.3   # < 0.3x ou > 3x do preço da API → provável misparse
PRECO_SUSPEITO_MAX = 3.0

COLS = ["item_key", "descricao_final", "fonte_descricao", "preco_api", "preco_pdf",
        "divergencia_preco", "fornecedor", "status"]

_tls = threading.local()


def _curador(cfg: dict, provedor: str) -> Curador:
    if not getattr(_tls, "c", None):
        _tls.c = Curador.from_provedor(cfg, provedor, max_retries=6)
    return _tls.c


def _num(v) -> float | None:
    """Converte número BR ('1.234,56') ou US ('1234.56') para float. None se não der."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    s = re.sub(r"[^\d.,-]", "", s)
    if "," in s:  # vírgula = separador decimal (BR): pontos são milhar
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def validar(match: dict, item: dict) -> tuple[str, float | None, float | None]:
    """Retorna (status, preco_pdf, divergencia) a partir do casamento com a tabela limpa."""
    if not (match.get("encontrado") and match.get("descricao_completa") and match.get("idx", -1) >= 0):
        return "nao_encontrado", None, None
    pe = _num(match.get("preco_unitario"))
    pa = _num(item.get("preco_unitario"))
    if pe is None or pe <= 0:
        return "pdf_ok_sem_preco", None, None
    if pa is None or pa == 0:
        return "pdf_ok_sem_ref", pe, None
    div = round((pe - pa) / pa, 4)
    if abs(div) <= 0.01:
        return "pdf_ok", pe, 0.0
    if pe < PRECO_SUSPEITO_MIN * abs(pa) or pe > PRECO_SUSPEITO_MAX * abs(pa):
        return "pdf_ok_preco_suspeito", pe, div
    return "pdf_ok_diverge", pe, div


def _linha(item: dict, match: dict, status: str, preco_pdf, div) -> dict:
    achou = status.startswith("pdf_ok")
    desc = (match.get("descricao_completa") or "").strip() if achou else ""
    return {
        "item_key": item.get("item_key", ""),
        "descricao_final": desc or item.get("descricao_api", ""),
        "fonte_descricao": "pdf" if desc else "api",
        "preco_api": item.get("preco_unitario", ""),
        "preco_pdf": "" if preco_pdf is None else preco_pdf,
        "divergencia_preco": "" if div is None else div,
        "fornecedor": (match.get("_fornecedor") or "") if achou else "",
        "status": status,
    }


def consolidar_destino():
    """Colapsa 5alt_itens_enriquecidos.csv em um destino por item_key (manter/revisar/descartar)."""
    linhas = ler_csv(str(SAIDA))
    out = []
    for r in linhas:
        st = r.get("status", "")
        destino = "manter" if st.startswith("pdf_ok") else "descartar"
        if st in ("pdf_ok_preco_suspeito",):
            destino = "revisar"
        out.append({
            "item_key": r.get("item_key", ""), "descricao_final": r.get("descricao_final", ""),
            "preco_api": r.get("preco_api", ""), "preco_pdf": r.get("preco_pdf", ""),
            "divergencia_preco": r.get("divergencia_preco", ""), "status": st, "destino": destino,
        })
    escrever_csv(str(DESTINO), ["item_key", "descricao_final", "preco_api", "preco_pdf",
                                "divergencia_preco", "status", "destino"], out)
    return out


def carregar_tabela_por_doc() -> dict:
    """Agrupa 5alt_tabela.csv por doc_key (ignora linhas-sentinela de doc sem tabela)."""
    por_doc = defaultdict(list)
    for r in ler_csv(str(TABELA)):
        if r.get("descricao", "").strip() == SENTINELA_VAZIO:
            por_doc.setdefault(r.get("doc_key", ""), [])
            continue
        por_doc[r.get("doc_key", "")].append(r)
    return por_doc


def main():
    ap = argparse.ArgumentParser(description="Etapa 5_alt_b — casa itens da API com a tabela limpa")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="openrouter")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()

    cfg = carregar_config()
    msg = exigir(cfg, args.provedor)
    if msg:
        raise SystemExit(msg)
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")
    if not TABELA.exists():
        raise SystemExit(f"{TABELA} ausente. Rode a 5_alt_a antes.")

    por_doc = carregar_tabela_por_doc()
    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    itens = sob.to_dict("records")
    # Só processa itens cujo documento tem tabela extraída (aparece em 5alt_tabela.csv).
    itens = [it for it in itens if it.get("pasta_arquivos", "") in por_doc]

    feitos = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [it for it in itens if it.get("item_key", "") not in feitos]
    if args.limite:
        pend = pend[: args.limite]
    if not pend:
        console.print("[5_alt_b] Nada a fazer. Consolidando destino…")
        consolidar_destino()
        return

    console.print(f"[bold][5_alt_b] {len(itens)} itens com tabela, já feitos: {len(feitos)}, "
                  f"pendentes: {len(pend)}[/] — concorrência: {args.concurrency}")

    with EscritorSeguro(str(SAIDA), COLS) as w:
        def fn(item):
            linhas = por_doc.get(item.get("pasta_arquivos", ""), [])
            if not linhas:
                return item, {"encontrado": False, "idx": -1}, "nao_encontrado", None, None
            match = _curador(cfg, args.provedor).casar_item_tabela(item, linhas)
            idx = match.get("idx", -1)
            if 0 <= idx < len(linhas):
                match["_fornecedor"] = linhas[idx].get("fornecedor", "")
            status, preco_pdf, div = validar(match, item)
            return item, match, status, preco_pdf, div

        def ok(_item, resultado):
            item, match, status, preco_pdf, div = resultado
            w.escrever(_linha(item, match, status, preco_pdf, div))

        def err(item, exc):
            w.escrever(_linha(item, {"encontrado": False, "idx": -1}, "erro", None, None))
            console.print(f"[yellow][5_alt_b] erro em {item.get('item_key','')}: {str(exc)[:100]}[/]")

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        )
        with progress:
            tarefa = progress.add_task("casando itens", total=len(pend))
            executar_paralelo(pend, fn, concurrency=args.concurrency, on_result=ok, on_error=err,
                              on_progress=lambda f, t: progress.update(tarefa, completed=f))

    dest = consolidar_destino()
    manter = sum(d["destino"] == "manter" for d in dest)
    revisar = sum(d["destino"] == "revisar" for d in dest)
    descartar = sum(d["destino"] == "descartar" for d in dest)
    diverge = sum(d["status"] == "pdf_ok_diverge" for d in dest)
    suspeito = sum(d["status"] == "pdf_ok_preco_suspeito" for d in dest)
    console.print(f"[bold green][5_alt_b] Concluído.[/] → {SAIDA}")
    console.print(f"[5_alt_b] Destino: manter={manter} revisar={revisar} descartar={descartar} "
                  f"(preço divergente={diverge}, suspeito={suspeito}) → {DESTINO}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow][5_alt_b] Interrompido — progresso salvo (resumível por item).[/]")
        sys.exit(130)
