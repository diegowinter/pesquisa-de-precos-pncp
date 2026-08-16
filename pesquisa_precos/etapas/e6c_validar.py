"""
Etapa 6c — LLM forte só na faixa ambígua do reranker + acúmulo de rótulos.

Só os pares `ambiguo` da 6b chegam aqui (tipicamente a minoria). Cada um vai ao modelo forte
(OPENAI_MODEL_PASS2) via comparar_par. Além disso, TODA decisão final — aceites/rejeições do
6b por threshold extremo E os vereditos do 6c — é appendada em data/6_rotulos_acumulados.csv,
que cresce entre execuções e serve para recalibrar thresholds / futuramente fine-tunar o reranker.

Entrada: data/6b_pares_rerankeados.csv (filtro ambiguo) + textos.
Saídas: data/6c_pares_validados.csv (par_key, mesmo_item, justificativa),
        data/6_rotulos_acumulados.csv (append, sem duplicar par_key já registrado).
Chave de resumo: par_key. Erros: erros/6c_erros.csv.
Uso: python -m pesquisa_precos.etapas.e6c_validar [--provedor openrouter] [--limite N]
"""

import argparse
import sys
import time

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
from pesquisa_precos.config.settings import carregar_config, exigir
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.providers.llm_curador import Curador
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.core.textos import descricao_itens, texto_catalogo

RERANK = paths.E6B_RERANKEADOS
CATALOGO = paths.E0A_CATALOGO
SOBREVIVENTES = paths.E4_SOBREVIVENTES
ENRIQUECIDOS = paths.E5_ENRIQUECIDOS
VALIDADOS = paths.E6C_VALIDADOS
ROTULOS = paths.E6_ROTULOS
ERROS = paths.ERROS_6C

COLS_VALID = ["par_key", "mesmo_item", "justificativa"]
COLS_ROTULO = ["par_key", "texto_catalogo", "texto_item", "score_rerank", "decisao_final", "origem", "timestamp"]


def main():
    ap = argparse.ArgumentParser(description="Etapa 6c — validação LLM dos ambíguos")
    ap.add_argument("--provedor", choices=["local", "openrouter"], default="openrouter")
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--fraco", action="store_true",
                    help="usa o modelo barato (PASS1/ling) em vez do forte (PASS2/qwen) na validação")
    args = ap.parse_args()
    forte = not args.fraco

    cfg = carregar_config()
    msg = exigir(cfg, args.provedor)
    if msg:
        raise SystemExit(msg)
    if not RERANK.exists():
        raise SystemExit(f"{RERANK} ausente. Rode a etapa 6b antes.")

    df = pd.read_csv(RERANK, dtype=str, encoding="utf-8").fillna("")
    cat = texto_catalogo(str(CATALOGO))
    itens = descricao_itens(str(SOBREVIVENTES), str(ENRIQUECIDOS))

    def t_cat(par_key):
        return cat.get(par_key.split("::", 1)[0], {}).get("texto", "")

    def t_itm(par_key):
        return itens.get(par_key.split("::", 1)[1], "")

    # ── Acúmulo de rótulos: registra as decisões extremas do 6b (origem=rerank) ──
    rotulos_existentes = ler_chaves_concluidas(str(ROTULOS), "par_key")
    esc_rot = EscritorSeguro(str(ROTULOS), COLS_ROTULO)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for _, r in df.iterrows():
        if r["decisao"] in ("aceito", "rejeitado") and r["par_key"] not in rotulos_existentes:
            esc_rot.escrever({
                "par_key": r["par_key"], "texto_catalogo": t_cat(r["par_key"])[:500],
                "texto_item": t_itm(r["par_key"])[:500], "score_rerank": r.get("score_rerank", ""),
                "decisao_final": r["decisao"], "origem": "rerank", "timestamp": ts,
            })
            rotulos_existentes.add(r["par_key"])

    # ── LLM nos ambíguos (resumível por par_key em 6c_pares_validados) ──
    ambiguos = df[df["decisao"] == "ambiguo"]
    feitas = ler_chaves_concluidas(str(VALIDADOS), "par_key")
    pend = [r for _, r in ambiguos.iterrows() if r["par_key"] not in feitas]
    if args.limite:
        pend = pend[: args.limite]
    print(f"[6c] Ambíguos: {len(ambiguos)} | a validar por LLM: {len(pend)}")

    if pend:
        modelo = cfg["model_pass2"] if forte else cfg["model_pass1"]
        print(f"[6c] modelo de validação: {modelo} ({'forte' if forte else 'fraco/barato'})")
        curador = Curador.from_provedor(cfg, args.provedor, forte=forte, max_retries=6)
        esc_val = EscritorSeguro(str(VALIDADOS), COLS_VALID)

        def fn(row):
            return curador.comparar_par(t_cat(row["par_key"]), t_itm(row["par_key"]))

        def ok(row, res):
            esc_val.escrever({"par_key": row["par_key"], "mesmo_item": res["mesmo_item"],
                              "justificativa": res["justificativa"]})
            if res["mesmo_item"] in ("sim", "nao") and row["par_key"] not in rotulos_existentes:
                esc_rot.escrever({
                    "par_key": row["par_key"], "texto_catalogo": t_cat(row["par_key"])[:500],
                    "texto_item": t_itm(row["par_key"])[:500], "score_rerank": row.get("score_rerank", ""),
                    "decisao_final": "aceito" if res["mesmo_item"] == "sim" else "rejeitado",
                    "origem": "llm", "timestamp": ts,
                })
                rotulos_existentes.add(row["par_key"])

        def err(row, exc):
            erros_log.logar_erro(str(ERROS), "6c", "", row["par_key"], "", exc)

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
            MofNCompleteColumn(), TimeElapsedColumn(), console=Console(),
        )
        with progress:
            tarefa = progress.add_task("validando ambíguos", total=len(pend))
            executar_paralelo(pend, fn, concurrency=args.concurrency, on_result=ok, on_error=err,
                              on_progress=lambda f, t: progress.update(tarefa, completed=f))
        esc_val.fechar()

    esc_rot.fechar()
    print(f"[6c] Concluído. Validados: {VALIDADOS} | rótulos: {ROTULOS}")


if __name__ == "__main__":
    main()
