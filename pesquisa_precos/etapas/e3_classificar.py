"""
Etapa 3 — Classificação de categoria por item do PNCP (LLM local, multi-label, O(textos)).

DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece milhares de
vezes). Classificamos cada texto ÚNICO (descrição, unidade) uma vez e espalhamos o rótulo para
todos os item_keys iguais — a saída continua por item_key (referência à ata/contrato intacta),
mas as chamadas de LLM caem de O(itens) para O(textos distintos). Item sem categoria de conteúdo
morre aqui (a "portaria de nomeação" nunca mais custa nada nas etapas seguintes).

Entrada: data/2_itens_coletados.csv (via coleta_pncp.carregar_itens_coletados). Para o aceite
da fase 2 sobre dados legados, use --entrada-legado com um CSV explodido da v1 (mapeia
item.descricao_item / numero_controle_pncp+item.numero_item).

Saída: data/3_itens_classificados.csv (item_key, categorias, confianca). Erros: erros/3_erros.csv.
Chave de resumo: item_key.
Uso: python -m pesquisa_precos.etapas.e3_classificar [--provedor local|openrouter] [--limite N]
"""

import argparse
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
from pesquisa_precos.core import erros_log
from pesquisa_precos.core.coleta import coleta_pncp
from pesquisa_precos.config.settings import carregar_config, exigir
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.providers.llm_curador import Curador
from pesquisa_precos.core.paralelo import executar_paralelo

console = Console()

ENTRADA = paths.E2_ITENS
CK_EXTRA = paths.CK_2_CONCEITOS_EXTRA
SAIDA = paths.E3_CLASSIFICADOS
ERROS = paths.ERROS_3

COLS = ["item_key", "categorias", "confianca"]


def carregar_itens(entrada_legado: str | None) -> pd.DataFrame:
    if entrada_legado:
        df = pd.read_csv(entrada_legado, dtype=str, encoding="utf-8-sig").fillna("")
        ctrl = df.get("numero_controle_pncp", pd.Series([""] * len(df)))
        num = df.get("item.numero_item", pd.Series([""] * len(df)))
        df = df.assign(
            item_key=[coleta_pncp.montar_item_key(c, n) for c, n in zip(ctrl, num)],
            descricao_api=df.get("item.descricao_item", ""),
            unidade=df.get("item.unidade_medida", ""),
        ).drop_duplicates(subset="item_key", keep="first")
        return df[["item_key", "descricao_api", "unidade"]]
    df = coleta_pncp.carregar_itens_coletados(str(ENTRADA), str(CK_EXTRA))
    if df.empty:
        raise SystemExit(f"{ENTRADA} vazio/ausente. Rode a etapa 2 (ou use --entrada-legado).")
    return df[["item_key", "descricao_api", "unidade"]]


def main():
    ap = argparse.ArgumentParser(description="Etapa 3 — classificação de itens PNCP")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="local")
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--entrada-legado", default=None, help="CSV explodido da v1 (aceite fase 2)")
    ap.add_argument("--retry-erros", action="store_true", help="Reprocessa só as chaves de erros/3_erros.csv")
    ap.add_argument("--reasoning", action="store_true",
                    help="Mantém o raciocínio do modelo LIGADO. Padrão: desligado (classificação é simples).")
    args = ap.parse_args()

    cfg = carregar_config()
    msg = exigir(cfg, args.provedor)
    if msg:
        raise SystemExit(msg)

    df = carregar_itens(args.entrada_legado)
    if args.retry_erros:
        # As linhas de erro foram gravadas na saída (confianca=erro) e contam como 'feitas',
        # então o filtro normal nunca as reprocessava (bug). Reescrevemos a saída SEM elas e
        # deixamos o fluxo normal reclassificar o que faltou — sem duplicar item_key.
        from pesquisa_precos.core.io_seguro import ler_csv, escrever_csv
        linhas = list(ler_csv(str(SAIDA)))
        limpas = [l for l in linhas if l.get("confianca") != "erro"]
        escrever_csv(str(SAIDA), COLS, limpas)
        console.print(f"[3] retry-erros: {len(linhas) - len(limpas)} linhas de erro removidas "
                      f"da saída para reprocessar.")
        feitas = {l["item_key"] for l in limpas}
    else:
        feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [r for _, r in df.iterrows() if r["item_key"] not in feitas]
    if not pend:
        console.print("[3] Nada a classificar (tudo já feito).")
        return

    # DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece
    # milhares de vezes — às vezes centenas dentro de um único contrato). Classificamos
    # cada texto ÚNICO uma vez e ESPALHAMOS o rótulo para todos os item_keys daquele texto.
    # A saída continua por item_key (referência à ata/contrato intacta); só as chamadas de
    # LLM caem de O(itens) para O(textos distintos). Chave = (descrição, unidade) — os dois
    # campos que o classificador usa, então texto igual ⇒ mesma classe, sem perda.
    def _norm(s):
        return " ".join(str(s or "").strip().lower().split())

    grupos: dict = {}
    for r in pend:
        chave = (_norm(r["descricao_api"]), _norm(r.get("unidade", "")))
        g = grupos.get(chave)
        if g is None:
            g = grupos[chave] = {"descricao_api": r["descricao_api"],
                                 "unidade": r.get("unidade", ""), "keys": []}
        g["keys"].append(r["item_key"])
    tarefas = list(grupos.values())
    n_textos = len(tarefas)
    if args.limite:
        tarefas = tarefas[: args.limite]
    n_itens = sum(len(g["keys"]) for g in tarefas)
    limite_txt = f" — rodando só {len(tarefas)} (limite)" if args.limite else ""
    console.print(f"[bold][3] Dedup: {len(pend)} itens → {n_textos} textos únicos[/]{limite_txt}\n"
                  f"    classificando {len(tarefas)} textos ({n_itens} itens), "
                  f"já feitos: {len(feitas)}, concorrência: {args.concurrency}")

    # Um Curador por thread: compartilhar um único cliente ChatOpenAI serializa as chamadas
    # HTTP e mata a concorrência (ver etapa 1). Cada worker cria o seu (thread-local).
    #
    # Desligar o raciocínio depende do servidor: o LM Studio (local) só respeita
    # `reasoning_effort: "none"` (top-level); o formato `reasoning:{enabled:false}` do
    # OpenRouter ele IGNORA. Modelo de raciocínio ligado custa ~5x o tempo por item.
    reasoning_kw = {}
    if not args.reasoning:
        reasoning_kw = ({"extra_body": {"reasoning_effort": "none"}} if args.provedor == "local"
                        else {"reasoning": {"enabled": False}})
    console.print(f"[dim][3] reasoning: {'ligado (default do modelo)' if args.reasoning else 'DESLIGADO'}[/]")
    _tls = threading.local()
    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = Curador.from_provedor(cfg, args.provedor, max_retries=6, **reasoning_kw)
        return _tls.c

    n_erros = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        # Cada tarefa é um GRUPO (texto único). Classifica uma vez e escreve o mesmo rótulo
        # para todos os item_keys do grupo (fan-out). As escritas rodam na thread principal
        # (ver paralelo.py), então gravar N item_keys de uma vez é seguro.
        def fn(g):
            return _curador().classificar_categoria(g["descricao_api"], g.get("unidade", ""))

        def ok(g, res):
            cats = "|".join(res["categorias"])
            conf = res.get("confianca", "")
            if conf == "erro":
                n_erros[0] += 1
                erros_log.logar_erro(str(ERROS), "3", "", g["keys"][0], g["descricao_api"], res.get("_erro"))
            for k in g["keys"]:
                w.escrever({"item_key": k, "categorias": cats, "confianca": conf})

        def err(g, exc):
            n_erros[0] += 1
            erros_log.logar_erro(str(ERROS), "3", "", g["keys"][0], g["descricao_api"], exc)
            for k in g["keys"]:
                w.escrever({"item_key": k, "categorias": "", "confianca": "erro"})

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        )
        with progress:
            tarefa = progress.add_task("classificando", total=len(tarefas))
            def prog(f, t):
                progress.update(tarefa, completed=f)
            executar_paralelo(tarefas, fn, concurrency=args.concurrency,
                              on_result=ok, on_error=err, on_progress=prog)
    cor = "yellow" if n_erros[0] else "green"
    console.print(f"[bold {cor}][3] Concluído.[/] {n_erros[0]} erros. → {SAIDA}")


if __name__ == "__main__":
    # KeyboardInterrupt = SIGINT (Ctrl+C ou sinal externo — terminal do VSCode, suspensão da
    # máquina). O progresso já está em disco (fsync por linha); saímos limpos com código 130
    # para o laço de auto-restart distinguir "interrompido" (relança) de "concluído" (exit 0).
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow][3] Interrompido (SIGINT) — progresso salvo, é resumível. "
                      "Rode de novo para continuar de onde parou.[/]")
        sys.exit(130)
