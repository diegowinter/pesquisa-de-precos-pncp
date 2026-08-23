"""
Etapa 6c — O LLM decide só a faixa ambígua do reranker, e toda decisão vira rótulo.

Só os pares que a 6b marcou como ambíguos chegam aqui — tipicamente a minoria. Cada um vai ao
LLM. Toda decisão final, tanto os aceites e rejeições que a 6b fechou por threshold quanto os
vereditos daqui, é acrescentada à tabela `label`, que cresce entre execuções e serve para
recalibrar os thresholds.

Restrição de custo (ADR-004): o modelo barato é o padrão, e o caro exige marcar `forte`. O
teto de custo do run é a segunda rede.

Não truncar `label`: é a base de calibração do projeto.
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.db import session as db
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "6c"
CODE_VERSION = "2.0.0"


class Params(BaseModel):
    provider: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Teto de pares ambíguos a validar (debug)")
    concurrency: int = Field(4, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    forte: bool = Field(
        False, description="Usa o model CARO (PASS2). Padrão é o barato — ver ADR-004.")


# ── Validação no banco (Fase 10) ────────────────────────────────────────────────────
#
# O veredito volta para a MESMA linha de `par` (ADR-013) e `recomputar_decisao_final()` fecha
# a decisão. `rotulo` continua sendo append-only: é o ativo de calibração do projeto e nunca
# pode ser truncado.
#
# RESTRIÇÃO DE CUSTO Nº 1 (ADR-004) vale igual aqui: o modelo barato é o padrão, `--forte`
# exige gesto explícito.

def _exigir_banco():

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


SQL_AMBIGUOS = """
    SELECT p.par_key,
           trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.description, '')),
           coalesce(NULLIF(e.descricao_final, ''), i.descricao_api),
           p.score_rerank
      FROM par p
      JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
      JOIN item i ON i.item_key = p.item_key
      LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
     WHERE p.decisao = 'ambiguo' AND p.veredito IS NULL
     ORDER BY p.par_key
"""


def _rodar(params: Params, ctx: RunContext) -> StepResult:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import par as repo_par

    forte = params.forte

    sql = SQL_AMBIGUOS + (f" LIMIT {int(params.limite)}" if params.limite else "")
    with db.session() as s:
        pend = s.execute(sa_text(sql)).all()
        contagens_antes = repo_par.contar(s)

    ctx.log("info", f"[6c] Ambíguos a validar por LLM: {len(pend)}")
    n_ok, n_erros = [0], [0]
    vereditos: dict[str, int] = {"sim": 0, "nao": 0, "indeterminado": 0}

    if pend:
        model = ctx.providers.resolucao("chat").info.model
        ctx.log("info" if not forte else "aviso",
                f"[6c] model de validação: {model} "
                f"({'FORTE/CARO — ver ADR-004' if forte else 'barato (padrão)'})")
        try:
            with db.session() as sessao:
                prompts_ativos = prompts_resolver.carregar_ativos(sessao, ["comparar_par"])
        except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
            prompts_ativos = {}
        curador = ctx.providers.novo_chat(
            curador_kwargs={"max_retries": 6, "prompts_ativos": prompts_ativos}).curador
        lote: list[tuple] = []

        def descarregar():
            if not lote:
                return
            with db.session() as s:
                repo_par.gravar_veredito(s, lote)
                s.commit()
            lote.clear()

        def fn(linha):
            return curador.comparar_par(linha[1] or "", linha[2] or "")

        def ok(linha, res):
            veredito = "sim" if res.get("mesmo_item") else "nao"
            vereditos[veredito] += 1
            n_ok[0] += 1
            lote.append((linha[0], veredito, (res.get("justificativa") or "")[:500], model))
            if len(lote) >= 200:
                descarregar()

        def err(linha, exc):
            n_erros[0] += 1
            ctx.erro_item(linha[0], exc)

        ctx.progresso(0, len(pend), descricao="validando ambíguos")
        try:
            executar_paralelo(pend, fn, concurrency=params.concurrency, on_result=ok,
                              on_error=err, on_progress=lambda f, t: ctx.progresso(f, t))
        finally:
            descarregar()   # o que já foi pago ao LLM é gravado mesmo se a etapa cair

    with db.session() as s:
        n_decisoes = repo_par.recomputar_decisao_final(s)
        s.commit()
        contagens = repo_par.contar(s)

    # `rotulo` acumula TODA decisão final (aceites/rejeições extremas do 6b + vereditos do 6c).
    # É o ativo de calibração do projeto — append-only, nunca truncado.
    n_rotulos = _acumular_rotulos(db)

    cor = "yellow" if n_erros[0] else "green"
    ctx.log("info", f"[bold {cor}][6c] Concluído.[/] vereditos={vereditos}, "
                    f"{n_erros[0]} erros · decisão final recomputada em {n_decisoes} pares · "
                    f"+{n_rotulos} rótulos")
    return StepResult(
        processed=n_ok[0], erros=n_erros[0],
        metrics={**vereditos, "decisoes_finais": n_decisoes, "rotulos_novos": n_rotulos,
                  "pares_antes": contagens_antes.get("par", 0), **contagens},
    )


SQL_ROTULOS_NOVOS = """
    INSERT INTO label (par_key, texto_catalogo, texto_item, score_rerank, final_decision,
                        source, model)
    SELECT p.par_key,
           left(trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.description, '')), 500),
           left(coalesce(NULLIF(e.descricao_final, ''), i.descricao_api), 500),
           p.score_rerank, p.final_decision::text,
           CASE WHEN p.veredito IS NOT NULL THEN 'llm' ELSE 'rerank' END,
           p.modelo_6c
      FROM par p
      JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
      JOIN item i ON i.item_key = p.item_key
      LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
     WHERE p.final_decision IN ('confirmado', 'rejeitado')
       AND NOT EXISTS (SELECT 1 FROM label r WHERE r.par_key = p.par_key)
"""


def _acumular_rotulos(db) -> int:
    """Registra em `label` toda decisão final que ainda não estava linhaá.

    `NOT EXISTS` em vez de `ON CONFLICT`: `label` não tem `par_key` único (um par pode ser
    rotulado de novo depois de uma recalibração), então a proteção contra duplicar tem que ser
    explícita na consulta.
    """
    with db.session() as s:
        n = s.execute(sa_text(SQL_ROTULOS_NOVOS)).rowcount
        s.commit()
    return n


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Uma chamada por par ambíguo ainda não validado."""

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.session() as s:
        n = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE decisao = 'ambiguo' AND veredito IS NULL")
        ).scalar_one()
        ambiguos = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE decisao = 'ambiguo'")).scalar_one()
    resolucao = ctx.providers.resolucao_opcional("chat")
    preco = resolucao.info.cost_usd_per_call if resolucao else None
    model = resolucao.info.model if resolucao else "— sem provider de `chat` configurado —"
    return Estimate(
        unidades=n, chamadas_llm=n,
        cost_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"ambiguos": ambiguos, "já_validados": ambiguos - n,
                  "model": f"{model} ({'CARO' if params.forte else 'barato'})"},
    )


def run(params: Params, ctx: RunContext) -> StepResult:
    return _rodar(params, ctx)
