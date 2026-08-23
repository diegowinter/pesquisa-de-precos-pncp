"""
Etapa 6b — O cross-encoder decide a maioria dos pares, sem gastar token.

Para cada par sobrevivente da 6a, o reranker pontua (texto do catálogo, descrição final do
item). A decisão sai por threshold: acima de `rerank_t_aceita` é aceito, abaixo de
`rerank_t_rejeita` é rejeitado, e o meio fica ambíguo para a etapa 6c resolver no LLM.

Trocar o modelo do reranker exige recalibrar os thresholds; a base para isso é a tabela
`label`, e a conta está em `tools/calibrate_thresholds.py`.
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "6b"
# 2.1.0 — Fase 14 (ADR-022): `t_aceita`/`t_rejeita` deixaram de vir do `.env` e viraram
# `Params`. O valor efetivo passa a sair de `config_version` (versionado e imutável), então o
# fingerprint TEM de enxergar a mudança de origem — daí o bump.
CODE_VERSION = "2.1.0"


class Params(BaseModel):
    limite: int | None = Field(None, description="Teto de pares a rerankear (debug)")
    batch: int = Field(16, ge=1, description="Tamanho do lote enviado ao reranker")
    # Os defaults são os que estavam no `.env` (RERANK_T_ACEITA / RERANK_T_REJEITA): a virada
    # não pode mudar resultado por si só. Para escolher outros com precisão/recall à vista,
    # ver a tela /recalibrate (`services.config.recalibrar_threshold`).
    rerank_t_aceita: float = Field(
        0.80, ge=0.0, le=1.0,
        description="Score do reranker que confirma o par direto, sem passar pela 6c")
    rerank_t_rejeita: float = Field(
        0.30, ge=0.0, le=1.0,
        description="Score do reranker que rejeita o par direto; entre os dois = ambíguo")


# ── Rerank no banco (Fase 10) ───────────────────────────────────────────────────────
#
# Os pares e os textos vêm do banco; a decisão volta para as MESMAS linhas de `par` (ADR-013:
# uma tabela, não três). A chave de resumo deixa de ser "par_key já no CSV" e passa a ser
# `par.score_rerank IS NULL` — derivada do próprio dado, como manda o ADR-018.

def _exigir_banco():
    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def run(params: Params, ctx: RunContext) -> StepResult:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import par as repo_par

    limite = f" LIMIT {int(params.limite)}" if params.limite else ""
    with db.session() as s:
        linhas = s.execute(sa_text(f"""
            SELECT p.par_key,
                   trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.description, '')),
                   coalesce(NULLIF(e.descricao_final, ''), i.descricao_api)
              FROM par p
              JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
              JOIN item i ON i.item_key = p.item_key
              LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
             WHERE p.sobreviveu AND p.score_rerank IS NULL
             ORDER BY p.par_key{limite}
        """)).all()

    if not linhas:
        ctx.log("info", "[6b] Nada a rerankear (todo par sobrevivente já tem score).")
        return StepResult()

    par_keys = [l[0] for l in linhas]
    pares_txt = [(l[1] or "", l[2] or "") for l in linhas]
    ctx.log("info", f"[6b] Rerankeando {len(pares_txt)} pares do banco"
                    "...")
    rer = ctx.providers.novo_rerank(batch=params.batch)

    passo = max(params.batch, 256)
    decisoes: dict[str, int] = {"aceito": 0, "rejeitado": 0, "ambiguo": 0}
    try:
        ctx.progresso(0, len(pares_txt), descricao="rerankeando")
        for i in range(0, len(pares_txt), passo):
            if ctx.cancelado():
                break
            scores = rer.score_pares(pares_txt[i:i + passo])
            lote = []
            for par_key, score in zip(par_keys[i:i + passo], scores):
                decisao = decidir(float(score), params.rerank_t_aceita,
                                  params.rerank_t_rejeita)
                decisoes[decisao] += 1
                lote.append((par_key, round(float(score), 4), decisao))
            # Grava a cada bloco, não no fim: o reranker é caro em GPU e uma queda no meio
            # não pode custar o que já foi calculado.
            with db.session() as s:
                repo_par.gravar_rerank(s, lote)
                s.commit()
            ctx.progresso(min(i + passo, len(pares_txt)))
    finally:
        rer.liberar()

    with db.session() as s:
        contagens = repo_par.contar(s)
    ctx.log("info", f"[bold green][6b] Concluído.[/] {decisoes} → tabela `par`")
    return StepResult(processed=sum(decisoes.values()), erros=0,
                          metrics={**decisoes, **contagens})


def decidir(score: float, t_aceita: float, t_rejeita: float) -> str:
    if score >= t_aceita:
        return "aceito"
    if score <= t_rejeita:
        return "rejeitado"
    return "ambiguo"


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Pares a rerankear. Roda na GPU: custo de token zero."""
    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.session() as s:
        n = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE sobreviveu AND score_rerank IS NULL")
        ).scalar_one()
    return Estimate(
        unidades=n, chamadas_llm=0, cost_usd=0.0,
        detalhes={"lotes": -(-n // max(params.batch, 1))},
    )
