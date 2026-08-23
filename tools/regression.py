"""
Suite de regressão de qualidade (Fase 9, docs/04_FASES.md item 1).

Roda a decisão da 6b (thresholds `rerank_t_aceita`/`rerank_t_rejeita`, sem LLM nenhum) contra
uma amostra de `label` (ou uma fixture sintética, se o banco estiver vazio/indisponível — hoje
é o caso: "ZERO linhas", ver CLAUDE.md) e reporta precisão/recall. É o que permite trocar de
modelo/threshold/prompt sem ser no escuro (docs/08_CONVENCOES.md §6).

A linhaógica de decisão vive em `pesquisa_precos.core.regression` (pura, sem I/O) — este script só
resolve DE ONDE vem a amostra e IMPRIME o relatório. `--reprovar-abaixo-de` faz o processo
sair com código 1 quando precisão OU recall ficam abaixo do limiar — é o que torna a suite
utilizável em CI/pre-play, não só como relatório manual.

Uso:
    python tools/regressao.py                              # banco, ou fixture se vazio
    python tools/regressao.py --fonte fixture               # força a fixture sintética
    python tools/regressao.py --t-aceita 0.80 --t-rejeita 0.30
    python tools/regressao.py --reprovar-abaixo-de 0.85     # sai com código 1 se reprovar
"""

import argparse
import csv
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from pesquisa_precos.core.regression import Label, avaliar  # noqa: E402

FIXTURE_PADRAO = RAIZ / "tests" / "fixtures" / "rotulos_sinteticos.csv"


def carregar_da_fixture(caminho: Path = FIXTURE_PADRAO) -> list[Label]:
    with open(caminho, encoding="utf-8") as f:
        return [
            Label(par_key=linha["par_key"],
                  score_rerank=float(linha["score_rerank"]) if linha["score_rerank"] else None,
                  final_decision=linha["final_decision"])
            for linha in csv.DictReader(f)
        ]


def carregar_do_banco(limite: int) -> list[Label] | None:
    """`None` se o banco estiver indisponível ou vazio — o chamador cai para a fixture."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import par as repo_par

    ok, _detalhe = db.is_available()
    if not ok:
        return None
    with db.session() as s:
        linhas = repo_par.amostra_rotulos(s, limite)
    if not linhas:
        return None
    return [Label(par_key=linha["par_key"], score_rerank=linha["score_rerank"],
                   final_decision=linha["final_decision"]) for linha in linhas]


def carregar_amostra(fonte: str, limite: int) -> tuple[list[Label], str]:
    if fonte == "fixture":
        return carregar_da_fixture(), "fixture sintética"
    if fonte == "banco":
        rotulos = carregar_do_banco(limite)
        if rotulos is None:
            raise SystemExit("Banco indisponível ou `label` vazio — rode com --fonte fixture.")
        return rotulos, "banco (label)"
    # 'auto' (default): banco quando disponível e não-vazio, senão a fixture — sem quebrar
    # quando o acervo ainda não foi migrado (CLAUDE.md: "o banco existe com ZERO linhas").
    rotulos = carregar_do_banco(limite)
    if rotulos is not None:
        return rotulos, "banco (label)"
    return carregar_da_fixture(), "fixture sintética (banco vazio/indisponível)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fonte", choices=["auto", "banco", "fixture"], default="auto")
    ap.add_argument("--limite", type=int, default=200, help="Tamanho máx. da amostra do banco")
    ap.add_argument("--t-aceita", type=float, default=0.80,
                    help="Threshold de aceite direto da 6b (default do .env: RERANK_T_ACEITA)")
    ap.add_argument("--t-rejeita", type=float, default=0.30,
                    help="Threshold de rejeição direta da 6b (default: RERANK_T_REJEITA)")
    ap.add_argument("--reprovar-abaixo-de", type=float, default=None,
                    help="Sai com código 1 se precisão OU recall ficarem abaixo deste limiar")
    args = ap.parse_args()

    rotulos, source = carregar_amostra(args.fonte, args.limite)
    resultado = avaliar(rotulos, t_aceita=args.t_aceita, t_rejeita=args.t_rejeita)

    print(f"Amostra: {resultado.n_amostra} rótulos ({source})")
    print(f"Thresholds: aceita≥{args.t_aceita}  rejeita<{args.t_rejeita}")
    print(f"Decididos pela 6b: {resultado.n_decididos}  |  ambíguos (iriam p/ 6c/LLM): "
          f"{resultado.n_ambiguos}")
    print(f"VP={resultado.verdadeiros_positivos} FP={resultado.falsos_positivos} "
          f"VN={resultado.verdadeiros_negativos} FN={resultado.falsos_negativos}")
    precisao = f"{resultado.precisao:.3f}" if resultado.precisao is not None else "n/d"
    recall = f"{resultado.recall:.3f}" if resultado.recall is not None else "n/d"
    print(f"Precisão: {precisao}   Recall: {recall}")

    if args.reprovar_abaixo_de is not None:
        limiar = args.reprovar_abaixo_de
        piores = [v for v in (resultado.precisao, resultado.recall) if v is not None]
        reprovado = not piores or any(v < limiar for v in piores)
        print(f"\n{'REPROVADO' if reprovado else 'APROVADO'} (limiar {limiar})")
        return 1 if reprovado else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
