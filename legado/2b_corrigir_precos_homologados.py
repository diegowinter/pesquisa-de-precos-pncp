"""
Etapa 2b (CORRETIVA, uso único) — troca o preço ESTIMADO pelo HOMOLOGADO nos itens já coletados.

Motivo: a etapa 2 gravava `valorUnitarioEstimado` como `preco_unitario`. Em muitas compras o
estimado é um placeholder (0,00 / 0,01) e o valor real (adjudicado) só existe no endpoint
/itens/{n}/resultados (`valorUnitarioHomologado`). Este script conserta o histórico SEM re-baixar
PDF nem chamar LLM. As próximas execuções já nascem certas (ver scripts/coleta_pncp.py).

Três fases (por padrão roda todas, resumíveis, seguras):
  1. backfill  → para cada item de 4_itens_sobreviventes, resolve a compra e busca o resultado
                 homologado vencedor. Grava data/2b_precos_homologados.csv (resumível por item_key).
  2. aplicar   → faz backup de 4_itens_sobreviventes.csv e reescreve: preco_unitario = homologado
                 (fallback ao estimado quando não há resultado); preserva o estimado em
                 preco_estimado e acrescenta fornecedor / data_resultado. Nada é perdido.
  3. repescar  → (opcional) faz backup de 5_itens_enriquecidos.csv e REMOVE as linhas dos itens
                 que antes falharam o match por preço-placeholder (estimado<=limiar e não
                 confirmados), para que um novo `python 5b_extrair_itens.py` os reprocesse já
                 ancorado no preço correto. Só PREPARA — o 5b você roda depois, no terminal.

Uso: python 2b_corrigir_precos_homologados.py [--concurrency 16] [--fases backfill,aplicar,repescar]
     [--repescar-limiar 1.0] [--limite N]
"""

import argparse
import re
import sys
import threading
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

from scripts import consultar_itens
from scripts.io_seguro import EscritorSeguro, ler_chaves_concluidas, ler_por_codigo
from scripts.paralelo import executar_paralelo

console = Console()

DATA = Path(__file__).resolve().parent / "data"
SOBREVIVENTES = DATA / "4_itens_sobreviventes.csv"
ENRIQUECIDOS = DATA / "5_itens_enriquecidos.csv"
PRECOS = DATA / "2b_precos_homologados.csv"

COLS_PRECO = ["item_key", "preco_estimado", "preco_homologado", "valor_total_homologado",
              "quantidade_homologada", "fornecedor", "ni_fornecedor", "data_resultado", "status"]

# controle: {cnpj}-{tipo}-{seq}/{ano}[-...]  → tipo, seq, ano
_CTRL = re.compile(r"-(\d+)-0*(\d+)/(\d{4})")


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def resolver_seq_compra(numero_controle: str, tipo_doc: str, cnpj: str,
                        cache: dict, lock: threading.Lock) -> tuple[str, str] | None:
    """Devolve (ano, seq_compra) para consultar itens/resultados, ou None se não der."""
    m = _CTRL.search(numero_controle or "")
    if not m:
        return None
    tipo, seq, ano = m.group(1), m.group(2), m.group(3)
    if tipo_doc == "contrato" or tipo == "2":
        chave = (cnpj, ano, seq)
        with lock:
            achado = cache.get(chave, "___")
        if achado == "___":
            achado = consultar_itens.resolver_sequencial_compra_contrato(cnpj, ano, seq, silent=True)
            with lock:
                cache[chave] = achado
        if not achado:
            return None
        return ano, achado
    return ano, seq


def fase_backfill(sob: pd.DataFrame, concurrency: int, limite: int | None):
    feitas = ler_chaves_concluidas(str(PRECOS), "item_key")
    pend = [r for _, r in sob.iterrows() if r["item_key"] not in feitas]
    if limite:
        pend = pend[:limite]
    console.print(f"[bold][2b/1] backfill homologado:[/] {len(pend)} itens a consultar "
                  f"(já feitos: {len(feitas)}), concorrência {concurrency}")
    if not pend:
        return

    cache_contrato: dict = {}
    lock = threading.Lock()

    def fn(item):
        est = _num(item.get("preco_unitario"))
        alvo = resolver_seq_compra(item["numeroControlePNCP"], item.get("tipo_doc", ""),
                                   item.get("orgao_cnpj", ""), cache_contrato, lock)
        base = {"item_key": item["item_key"], "preco_estimado": "" if est is None else est,
                "preco_homologado": "", "valor_total_homologado": "", "quantidade_homologada": "",
                "fornecedor": "", "ni_fornecedor": "", "data_resultado": "", "status": ""}
        if not alvo:
            return {**base, "status": "sem_seq"}
        ano, seq = alvo
        res = consultar_itens.fetch_resultado_vencedor(
            item.get("orgao_cnpj", ""), ano, seq, item.get("numeroItem", ""), silent=True)
        if not res:
            return {**base, "status": "sem_resultado"}
        return {**base,
                "preco_homologado": res.get("valorUnitarioHomologado", ""),
                "valor_total_homologado": res.get("valorTotalHomologado", ""),
                "quantidade_homologada": res.get("quantidadeHomologada", ""),
                "fornecedor": res.get("nomeRazaoSocialFornecedor", "") or "",
                "ni_fornecedor": res.get("niFornecedor", "") or "",
                "data_resultado": res.get("dataResultado", "") or "",
                "status": "homologado"}

    with EscritorSeguro(str(PRECOS), COLS_PRECO) as w:
        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        )
        with progress:
            tarefa = progress.add_task("consultando resultado homologado", total=len(pend))
            executar_paralelo(
                pend, fn, concurrency=concurrency,
                on_result=lambda item, res: w.escrever(res),
                on_error=lambda item, exc: w.escrever(
                    {"item_key": item["item_key"], "status": f"erro:{type(exc).__name__}"}),
                on_progress=lambda f, t: progress.update(tarefa, completed=f))


def fase_aplicar(sob: pd.DataFrame) -> pd.DataFrame:
    precos = ler_por_codigo(str(PRECOS), "item_key")
    if not precos:
        console.print("[yellow][2b/2] sem 2b_precos_homologados.csv — rode o backfill antes.[/]")
        return sob

    bak = SOBREVIVENTES.with_suffix(".bak.csv")
    if not bak.exists():
        sob.to_csv(bak, index=False, encoding="utf-8")
        console.print(f"[2b/2] backup salvo: {bak.name}")

    df = sob.copy()
    if "preco_estimado" not in df.columns:
        df.insert(df.columns.get_loc("preco_unitario") + 1, "preco_estimado", df["preco_unitario"])
    for c in ("fornecedor", "data_resultado"):
        if c not in df.columns:
            df[c] = ""

    n_trocados = 0
    novos_preco, novos_forn, novos_data = [], [], []
    for _, r in df.iterrows():
        p = precos.get(r["item_key"], {})
        homo = _num(p.get("preco_homologado"))
        if homo is not None and homo > 0:
            novos_preco.append(f"{homo}")
            novos_forn.append(p.get("fornecedor", "") or r.get("fornecedor", ""))
            novos_data.append(p.get("data_resultado", "") or r.get("data_resultado", ""))
            n_trocados += 1
        else:
            novos_preco.append(r["preco_unitario"])
            novos_forn.append(r.get("fornecedor", ""))
            novos_data.append(r.get("data_resultado", ""))
    df["preco_unitario"], df["fornecedor"], df["data_resultado"] = novos_preco, novos_forn, novos_data
    df.to_csv(SOBREVIVENTES, index=False, encoding="utf-8")
    console.print(f"[bold green][2b/2] aplicado.[/] preço homologado em {n_trocados} itens "
                  f"(demais mantêm o estimado). → {SOBREVIVENTES.name}")
    return df


def fase_repescar(limiar: float):
    if not ENRIQUECIDOS.exists():
        console.print("[yellow][2b/3] 5_itens_enriquecidos.csv ausente — pulei a repescagem.[/]")
        return
    precos = ler_por_codigo(str(PRECOS), "item_key")
    enr = pd.read_csv(ENRIQUECIDOS, dtype=str, encoding="utf-8").fillna("")

    def _tem_homo(ik):
        homo = _num(precos.get(ik, {}).get("preco_homologado"))
        return homo is not None and homo > 0

    def _est_baixo(ik):
        est = _num(precos.get(ik, {}).get("preco_estimado"))
        return est is not None and est <= limiar

    nao_conf = enr["enriquecimento"].isin(["qtd_nao_confere", "nao_encontrado"])
    alvo = enr[nao_conf & enr["item_key"].map(lambda ik: _est_baixo(ik) and _tem_homo(ik))]
    if alvo.empty:
        console.print("[2b/3] nenhum item elegível para repescagem — nada a fazer.")
        return

    bak = ENRIQUECIDOS.with_suffix(".bak.csv")
    if not bak.exists():
        enr.to_csv(bak, index=False, encoding="utf-8")
        console.print(f"[2b/3] backup salvo: {bak.name}")
    restante = enr[~enr["item_key"].isin(set(alvo["item_key"]))]
    restante.to_csv(ENRIQUECIDOS, index=False, encoding="utf-8")
    console.print(f"[bold green][2b/3] repescagem preparada:[/] removi {len(alvo)} itens de "
                  f"{ENRIQUECIDOS.name} (backup feito). Agora rode o 5b de novo para reprocessá-los:")
    console.print("   [cyan]python 5b_extrair_itens.py --provedor openrouter --concurrency 16[/]")


def main():
    ap = argparse.ArgumentParser(description="Etapa 2b corretiva — preço homologado")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--fases", default="backfill,aplicar,repescar",
                    help="subconjunto de: backfill,aplicar,repescar")
    ap.add_argument("--repescar-limiar", type=float, default=1.0)
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()

    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente.")
    fases = {f.strip() for f in args.fases.split(",") if f.strip()}

    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    if "backfill" in fases:
        fase_backfill(sob, args.concurrency, args.limite)
    if "aplicar" in fases:
        sob = fase_aplicar(sob)
    if "repescar" in fases:
        fase_repescar(args.repescar_limiar)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow][2b] Interrompido — progresso do backfill salvo (resumível).[/]")
        sys.exit(130)
