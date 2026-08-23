"""
Diff entre runs (Fase 9, docs/04_FASES.md item 2) — "o que mudou do export de ontem para o de
hoje": item novo, item sumiu, preço mudou. Generaliza a lógica de `--novos`/`export_snapshot`
da etapa 8 (compara chaves entre dois conjuntos) para comparar dois `grupo_item` de runs
DIFERENTES, em vez de um run contra o snapshot do último `--novos`.

`api/` e `web/` só chamam este módulo (docs/01_ARQUITETURA.md §7) — nenhum SQL solto fora de
`db/repos`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import grupo as repo_grupo


class RunSemRankingError(RuntimeError):
    """Run não tem linhas em `grupo_item` — não há o que comparar (etapa 7 não rodou nele)."""


def _chave(linha: dict) -> tuple[str, str, int]:
    """Identidade de uma linha do ranking: (código, nº controle PNCP, item) — a mesma chave
    de `export`/`export_snapshot` (etapas 7/8), só que agora comparando dois runs quaisquer."""
    return (linha["codigo"], linha["numero_controle_pncp"], linha["numero_item"])


def _preco(linha: dict) -> Decimal | None:
    return linha.get("preco_unitario")


def diff_runs(run_a_id: int, run_b_id: int, *, limiar_variacao: float = 0.0) -> dict[str, Any]:
    """Compara o ranking de `run_a` (mais antigo, "ontem") contra `run_b` (mais novo, "hoje").

    `limiar_variacao` filtra ruído de arredondamento nas "preço mudou" — só entra quem variou
    mais que essa fração (0.0 = qualquer variação de centavo conta).
    """
    with db.session() as sessao:
        linhas_a = repo_grupo.linhas_do_run(sessao, run_a_id)
        linhas_b = repo_grupo.linhas_do_run(sessao, run_b_id)
    if not linhas_a:
        raise RunSemRankingError(f"run #{run_a_id} não tem linhas em grupo_item (rode a step 7)")
    if not linhas_b:
        raise RunSemRankingError(f"run #{run_b_id} não tem linhas em grupo_item (rode a step 7)")

    por_chave_a = {_chave(l): l for l in linhas_a}
    por_chave_b = {_chave(l): l for l in linhas_b}
    chaves_a, chaves_b = set(por_chave_a), set(por_chave_b)

    itens_novos = sorted(chaves_b - chaves_a)
    itens_sumidos = sorted(chaves_a - chaves_b)

    precos_mudaram: list[dict[str, Any]] = []
    for key in sorted(chaves_a & chaves_b):
        la, lb = por_chave_a[key], por_chave_b[key]
        pa, pb = _preco(la), _preco(lb)
        if pa is None or pb is None or pa == pb:
            continue
        variacao = float(pb - pa) / float(pa) if pa else None
        if variacao is not None and abs(variacao) <= limiar_variacao:
            continue
        precos_mudaram.append({
            "codigo": key[0], "numero_controle_pncp": key[1], "numero_item": key[2],
            "preco_antes": pa, "preco_depois": pb, "variacao": variacao,
        })

    def _linhas(chaves: list[tuple], source: dict[tuple, dict]) -> list[dict]:
        return [{"codigo": c, "numero_controle_pncp": nc, "numero_item": ni,
                 "preco_unitario": source[(c, nc, ni)].get("preco_unitario"),
                 "descricao_final": source[(c, nc, ni)].get("descricao_final")}
                for c, nc, ni in chaves]

    return {
        "run_a": run_a_id, "run_b": run_b_id,
        "total_run_a": len(linhas_a), "total_run_b": len(linhas_b),
        "n_novos": len(itens_novos), "n_sumidos": len(itens_sumidos),
        "n_preco_mudou": len(precos_mudaram),
        "itens_novos": _linhas(itens_novos, por_chave_b),
        "itens_sumidos": _linhas(itens_sumidos, por_chave_a),
        "precos_mudaram": precos_mudaram,
    }
