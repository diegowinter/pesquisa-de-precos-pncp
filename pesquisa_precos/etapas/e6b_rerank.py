"""
Etapa 6b — Reranker (cross-encoder) decide a maioria dos pares, custo zero de token.

Para cada par sobrevivente da 6a: score do cross-encoder bge-reranker sobre (texto_catalogo,
descricao_final do item). Decisão por threshold:
  score >= RERANK_T_ACEITA → aceito;  score <= RERANK_T_REJEITA → rejeitado;  entre → ambiguo.

Entrada: data/6a_pares_candidatos.csv (sobreviveu=true) + textos das saídas anteriores.
Saída: data/6b_pares_rerankeados.csv (par_key, score_rerank, decisao). Chave de resumo: par_key.
GPU: o reranker roda sozinho.

NÃO fazer: trocar o modelo do reranker sem recalibrar os thresholds — a base para isso é
data/6_rotulos_acumulados.csv (ver ferramentas/calibrar_thresholds.py).

Uso: python -m pesquisa_precos.etapas.e6b_rerank [--limite N] [--remoto]
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "6b"
VERSAO_CODIGO = "2.0.0"


class Params(BaseModel):
    limite: int | None = Field(None, description="Teto de pares a rerankear (debug)")
    batch: int = Field(16, ge=1, description="Tamanho do lote enviado ao reranker")
    remoto: bool = Field(
        False, description="Usa o servidor de GPU (GPU_BASE_URL) para o reranker")


# ── Rerank no banco (Fase 10) ───────────────────────────────────────────────────────
#
# Os pares e os textos vêm do banco; a decisão volta para as MESMAS linhas de `par` (ADR-013:
# uma tabela, não três). A chave de resumo deixa de ser "par_key já no CSV" e passa a ser
# `par.score_rerank IS NULL` — derivada do próprio dado, como manda o ADR-018.

def _exigir_banco():
    from pesquisa_precos.db import sessao as db

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import par as repo_par

    cfg = ctx.config
    limite = f" LIMIT {int(params.limite)}" if params.limite else ""
    with db.sessao() as s:
        linhas = s.execute(sa_text(f"""
            SELECT p.par_key,
                   trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.descricao, '')),
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
        return ResultadoEtapa()

    par_keys = [l[0] for l in linhas]
    pares_txt = [(l[1] or "", l[2] or "") for l in linhas]
    ctx.log("info", f"[6b] Rerankeando {len(pares_txt)} pares do banco"
                    f"{' (remoto)' if params.remoto else ''}...")
    rer = ctx.provedores.novo_rerank(remoto=params.remoto, batch=params.batch)

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
                decisao = decidir(float(score), cfg["rerank_t_aceita"],
                                  cfg["rerank_t_rejeita"])
                decisoes[decisao] += 1
                lote.append((par_key, round(float(score), 4), decisao))
            # Grava a cada bloco, não no fim: o reranker é caro em GPU e uma queda no meio
            # não pode custar o que já foi calculado.
            with db.sessao() as s:
                repo_par.gravar_rerank(s, lote)
                s.commit()
            ctx.progresso(min(i + passo, len(pares_txt)))
    finally:
        rer.liberar()

    with db.sessao() as s:
        contagens = repo_par.contar(s)
    ctx.log("info", f"[bold green][6b] Concluído.[/] {decisoes} → tabela `par`")
    return ResultadoEtapa(processados=sum(decisoes.values()), erros=0,
                          metricas={**decisoes, **contagens})


def decidir(score: float, t_aceita: float, t_rejeita: float) -> str:
    if score >= t_aceita:
        return "aceito"
    if score <= t_rejeita:
        return "rejeitado"
    return "ambiguo"


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Pares a rerankear. Roda na GPU: custo de token zero."""
    from pesquisa_precos.db import sessao as db

    ok, detalhe = db.esta_disponivel()
    if not ok:
        return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.sessao() as s:
        n = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE sobreviveu AND score_rerank IS NULL")
        ).scalar_one()
    return Estimativa(
        unidades=n, chamadas_llm=0, custo_usd=0.0,
        detalhes={"lotes": -(-n // max(params.batch, 1)),
                  "reranker": "GPU remota" if params.remoto else "local"},
    )
