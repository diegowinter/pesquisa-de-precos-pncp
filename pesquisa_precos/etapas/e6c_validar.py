"""
Etapa 6c — LLM só na faixa ambígua do reranker + acúmulo de rótulos.

Só os pares `ambiguo` da 6b chegam aqui (tipicamente a minoria: 57k de 250k no acervo atual).
Cada um vai ao LLM via comparar_par. Além disso, TODA decisão final — aceites/rejeições do 6b
por threshold extremo E os vereditos do 6c — é appendada em data/6_rotulos_acumulados.csv, que
cresce entre execuções e serve para recalibrar thresholds / futuramente fine-tunar o reranker.

⚠ RESTRIÇÃO DE CUSTO Nº 1 (ADR-004). Até a Fase 0 esta etapa usava o modelo CARO por padrão e
só usava o barato com `--fraco` — comportamento seguro dependia de alguém lembrar de digitar
uma flag. Aqui isso está invertido: **o modelo barato (PASS1) é o padrão** e o caro exige
`--forte` explícito. `--fraco` continua aceito, sem efeito, para não quebrar o comando que já
está no histórico do terminal do operador.

Entrada: data/6b_pares_rerankeados.csv (filtro ambiguo) + textos.
Saídas: data/6c_pares_validados.csv (par_key, mesmo_item, justificativa),
        data/6_rotulos_acumulados.csv (append, sem duplicar par_key já registrado).
Chave de resumo: par_key. Erros: erros/6c_erros.csv.

NÃO fazer: truncar 6_rotulos_acumulados.csv — é o ativo de calibração do projeto.

Uso: python -m pesquisa_precos.etapas.e6c_validar [--provedor openrouter] [--limite N]
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.config.settings import custo_por_chamada, exigir
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.db import sessao as db
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "6c"
VERSAO_CODIGO = "2.0.0"


class Params(BaseModel):
    provedor: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    limite: int | None = Field(None, description="Teto de pares ambíguos a validar (debug)")
    concurrency: int = Field(4, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    forte: bool = Field(
        False, description="Usa o modelo CARO (PASS2). Padrão é o barato — ver ADR-004.")
    fraco: bool = Field(
        False, description="(obsoleta) O modelo barato já é o padrão; a flag não faz nada.")


# ── Validação no banco (Fase 10) ────────────────────────────────────────────────────
#
# O veredito volta para a MESMA linha de `par` (ADR-013) e `recomputar_decisao_final()` fecha
# a decisão. `rotulo` continua sendo append-only: é o ativo de calibração do projeto e nunca
# pode ser truncado.
#
# RESTRIÇÃO DE CUSTO Nº 1 (ADR-004) vale igual aqui: o modelo barato é o padrão, `--forte`
# exige gesto explícito.

def _exigir_banco():

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


SQL_AMBIGUOS = """
    SELECT p.par_key,
           trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.descricao, '')),
           coalesce(NULLIF(e.descricao_final, ''), i.descricao_api),
           p.score_rerank
      FROM par p
      JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
      JOIN item i ON i.item_key = p.item_key
      LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
     WHERE p.decisao = 'ambiguo' AND p.veredito IS NULL
     ORDER BY p.par_key
"""


def _rodar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import par as repo_par

    cfg = ctx.config
    forte = params.forte
    if params.fraco:
        ctx.log("debug", "[dim][6c] --fraco é obsoleta: o modelo barato já é o padrão.[/]")

    sql = SQL_AMBIGUOS + (f" LIMIT {int(params.limite)}" if params.limite else "")
    with db.sessao() as s:
        pend = s.execute(sa_text(sql)).all()
        contagens_antes = repo_par.contar(s)

    ctx.log("info", f"[6c] Ambíguos a validar por LLM: {len(pend)}")
    n_ok, n_erros = [0], [0]
    vereditos: dict[str, int] = {"sim": 0, "nao": 0, "indeterminado": 0}

    if pend:
        modelo = cfg["model_pass2"] if forte else cfg["model_pass1"]
        ctx.log("info" if not forte else "aviso",
                f"[6c] modelo de validação: {modelo} "
                f"({'FORTE/CARO — ver ADR-004' if forte else 'barato (padrão)'})")
        try:
            with db.sessao() as sessao:
                prompts_ativos = prompts_resolver.carregar_ativos(sessao, ["comparar_par"])
        except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
            prompts_ativos = {}
        curador = Curador.from_provedor(cfg, params.provedor, forte=forte, max_retries=6,
                                        prompts_ativos=prompts_ativos)
        lote: list[tuple] = []

        def descarregar():
            if not lote:
                return
            with db.sessao() as s:
                repo_par.gravar_veredito(s, lote)
                s.commit()
            lote.clear()

        def fn(linha):
            return curador.comparar_par(linha[1] or "", linha[2] or "")

        def ok(linha, res):
            veredito = "sim" if res.get("mesmo_item") else "nao"
            vereditos[veredito] += 1
            n_ok[0] += 1
            lote.append((linha[0], veredito, (res.get("justificativa") or "")[:500], modelo))
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

    with db.sessao() as s:
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
    return ResultadoEtapa(
        processados=n_ok[0], erros=n_erros[0],
        metricas={**vereditos, "decisoes_finais": n_decisoes, "rotulos_novos": n_rotulos,
                  "pares_antes": contagens_antes.get("par", 0), **contagens},
    )


SQL_ROTULOS_NOVOS = """
    INSERT INTO rotulo (par_key, texto_catalogo, texto_item, score_rerank, decisao_final,
                        origem, modelo)
    SELECT p.par_key,
           left(trim(coalesce(c.nome_pdm, '') || ' ' || coalesce(c.descricao, '')), 500),
           left(coalesce(NULLIF(e.descricao_final, ''), i.descricao_api), 500),
           p.score_rerank, p.decisao_final::text,
           CASE WHEN p.veredito IS NOT NULL THEN 'llm' ELSE 'rerank' END,
           p.modelo_6c
      FROM par p
      JOIN catalogo_item c ON c.tipo = p.tipo AND c.codigo = p.codigo
      JOIN item i ON i.item_key = p.item_key
      LEFT JOIN item_enriquecido e ON e.item_key = p.item_key
     WHERE p.decisao_final IN ('confirmado', 'rejeitado')
       AND NOT EXISTS (SELECT 1 FROM rotulo r WHERE r.par_key = p.par_key)
"""


def _acumular_rotulos(db) -> int:
    """Registra em `rotulo` toda decisão final que ainda não estava lá.

    `NOT EXISTS` em vez de `ON CONFLICT`: `rotulo` não tem `par_key` único (um par pode ser
    rotulado de novo depois de uma recalibração), então a proteção contra duplicar tem que ser
    explícita na consulta.
    """
    with db.sessao() as s:
        n = s.execute(sa_text(SQL_ROTULOS_NOVOS)).rowcount
        s.commit()
    return n


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada por par ambíguo ainda não validado."""

    ok, detalhe = db.esta_disponivel()
    if not ok:
        return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.sessao() as s:
        n = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE decisao = 'ambiguo' AND veredito IS NULL")
        ).scalar_one()
        ambiguos = s.execute(sa_text(
            "SELECT count(*) FROM par WHERE decisao = 'ambiguo'")).scalar_one()
    preco = custo_por_chamada(ctx.config, params.provedor, forte=params.forte)
    modelo = ctx.config["model_pass2"] if params.forte else ctx.config["model_pass1"]
    return Estimativa(
        unidades=n, chamadas_llm=n,
        custo_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"ambiguos": ambiguos, "já_validados": ambiguos - n,
                  "modelo": f"{modelo} ({'CARO' if params.forte else 'barato'})"},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    msg = exigir(ctx.config, params.provedor)
    if msg:
        raise SystemExit(msg)
    return _rodar(params, ctx)
