"""
Etapa 3 — Classificação de categoria por item do PNCP (LLM, multi-label, O(textos)).

DEDUP: a descrição do PNCP é canônica e se repete MUITO (o mesmo texto reaparece milhares de
vezes). Classificamos cada texto ÚNICO (descrição, unidade) uma vez e espalhamos o rótulo para
todos os item_keys iguais — a saída continua por item_key (referência à ata/contrato intacta),
mas as chamadas de LLM caem de O(itens) para O(textos distintos). Item sem categoria de conteúdo
morre aqui (a "portaria de nomeação" nunca mais custa nada nas etapas seguintes).

Entrada: data/2_itens_coletados.csv (via collect_pncp.carregar_itens_coletados). Para o aceite
sobre dados legados, use --entrada-legado com um CSV explodido da v1 (mapeia
item.descricao_item / numero_controle_pncp+item.numero_item).

Saída: data/3_itens_classificados.csv (item_key, categorias, confianca). Erros: erros/3_erros.csv.
Chave de resumo: item_key.

NÃO fazer: classificar por item em vez de por texto único — é o dedup de ~5x que segura o
custo desta etapa, a mais cara do ciclo.

Uso: python -m pesquisa_precos.steps.e3_classify [--provider local|openrouter] [--limite N]
"""

import sys
import threading

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel, Field

from pesquisa_precos.core.parallel import executar_paralelo
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.db import session as db
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "3"
# 1.1.0 (Fase 2): o dedup passa a agrupar pelo `texto_hash` canônico de core.text, que
# dobra acento — antes o agrupamento era por (lower, espaços colapsados) sem dobra.
CODE_VERSION = "2.0.0"


class Params(BaseModel):
    provider: str | None = Field(
        None, description="Override manual do provider de chat [local|openrouter]. "
        "Sem valor, usa o que estiver configurado em provider_capability (Fase 7) — ou "
        "'local' se o banco de provedores ainda não tiver sido configurado (ADR-014).")
    limite: int | None = Field(None, description="Teto de textos únicos a classificar (debug)")
    concurrency: int = Field(3, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    retry_erros: bool = Field(
        False, description="Reprocessa só as chaves de erros/3_erros.csv")
    reasoning: bool = Field(
        False, description="Mantém o raciocínio do model LIGADO. Padrão: desligado.")


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Uma chamada por TEXTO único ainda não classificado — não por item."""
    from pesquisa_precos.db.repos import classification as repo

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    with db.session() as s:
        n_textos, n_itens = repo.contar_pendentes(s)
        ja = len(repo.hashes_ja_classificados(s))
    n = n_textos if not params.limite else min(n_textos, params.limite)
    resolucao = ctx.providers.resolucao_opcional("chat")
    preco = resolucao.info.cost_usd_per_call if resolucao else None
    return Estimate(
        unidades=n, chamadas_llm=n,
        cost_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"itens_pendentes": n_itens,
                  "textos_unicos": n_textos,
                  "dedup": f"{n_itens / max(n_textos, 1):.1f}x",
                  "textos_ja_classificados (nunca repagos)": ja},
    )


def run(params: Params, ctx: RunContext) -> StepResult:
    # Fase 14 (ADR-022): uma fonte só. `resolucao` levanta `CapabilityNotConfigured` se
    # ninguém atende `chat` — a validação de `.env` que existia aqui virou desnecessária.
    resolucao_chat = ctx.providers.resolucao("chat")
    nome_provedor = resolucao_chat.info.name

    # Prompt e reasoning são resolvidos igual nos dois caminhos; só o IO muda.
    reasoning_kw = {}
    if not params.reasoning:
        reasoning_kw = ({"extra_body": {"reasoning_effort": "none"}}
                        if nome_provedor == "local" else {"reasoning": {"enabled": False}})
    try:
        with db.session() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(sessao,
                                                              ["classificar_item"])
    except Exception:  # noqa: BLE001 — sem banco de prompts, cai no hardcoded
        prompts_ativos = {}
    return _rodar(params, ctx, resolucao_chat, prompts_ativos, reasoning_kw)


# ── Classificação no banco (Fase 10) ────────────────────────────────────────────────
#
# O dedup por texto — o que segura o custo desta etapa, a mais cara do ciclo — deixa de ser
# intra-execução e vira PERMANENTE (ADR-007): `texto_classificacao` sobrevive entre runs, e
# um texto já pago nunca mais volta ao model. No CSV, o agrupamento era refeito a cada
# execução sobre 1,6 milhão de linhas em memória; aqui o `texto_hash` já veio calculado da
# ingestão da etapa 2 e o agrupamento é do banco.

def _rodar(params: Params, ctx: RunContext, resolucao_chat,
           prompts_ativos: dict, reasoning_kw: dict) -> StepResult:
    from pesquisa_precos.db.repos import classification as repo

    ok_banco, detalhe = db.is_available()
    if not ok_banco:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")

    with db.session() as s:
        n_textos, n_itens_pend = repo.contar_pendentes(s)
        tarefas = repo.textos_pendentes(s, params.limite)
    if not tarefas:
        with db.session() as s:
            n_recomputadas = repo.recomputar_item_categoria(s)
            s.commit()
        ctx.log("info", "[3] Nada a classificar (todo texto já está em texto_classificacao).")
        return StepResult(metrics={"textos_ja_classificados": 0,
                                        "item_categoria_recomputadas": n_recomputadas})

    n_itens = sum(g["n_itens"] for g in tarefas)
    limite_txt = f" — rodando só {len(tarefas)} (limite)" if params.limite else ""
    ctx.log("info", f"[bold][3] Dedup: {n_itens_pend} itens → {n_textos} textos únicos[/]"
                    f"{limite_txt} · classificando {len(tarefas)} textos "
                    f"({n_itens} itens), concorrência: {params.concurrency}")

    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = ctx.providers.novo_chat(
                curador_kwargs={"prompts_ativos": prompts_ativos, **reasoning_kw}).curador
        return _tls.c

    nome_provedor = resolucao_chat.info.name
    model = getattr(resolucao_chat.info, "model", None) or nome_provedor
    n_erros, n_ok = [0], [0]
    lote: list[tuple] = []

    def descarregar():
        """Grava o lote acumulado. Em lote e não por texto: `COPY` numa transação por item
        seria mais lento que a própria chamada de LLM que estamos economizando."""
        if not lote:
            return
        with db.raw_connection() as conn:
            repo.gravar(conn, lote)
            conn.commit()
        lote.clear()

    def fn(g):
        return _curador().classificar_categoria(g["descricao"], g.get("unidade") or "")

    def ok(g, res):
        conf = res.get("confianca", "")
        if conf == "erro":
            n_erros[0] += 1
            ctx.erro_item(g["texto_hash"], res.get("_erro"), name=g["descricao"])
            return   # texto com erro NÃO entra na tabela: entrar marcaria como pago algo
                     # que não foi classificado, e o retry nunca mais o encontraria.
        n_ok[0] += 1
        # `confianca` é `real` no banco e PALAVRA no LLM — a escala ordinal é declarada em
        # `repo.CONFIANCA_ORDINAL`, a mesma que a migração usa.
        lote.append((g["texto_hash"], g["descricao"], g.get("unidade"),
                     res["categorias"], repo.confianca_para_real(conf),
                     res.get("_prompt_versao_id"), model, nome_provedor, None))
        if len(lote) >= 500:
            descarregar()

    def err(g, exc):
        n_erros[0] += 1
        ctx.erro_item(g["texto_hash"], exc, name=g["descricao"])

    ctx.progresso(0, len(tarefas), descricao="classificando")
    try:
        executar_paralelo(tarefas, fn, concurrency=params.concurrency,
                          on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    finally:
        descarregar()   # o que já foi pago é gravado mesmo se a etapa cair no meio

    with db.session() as s:
        n_recomputadas = repo.recomputar_item_categoria(s)
        s.commit()
        contagens = repo.contar(s)

    cor = "yellow" if n_erros[0] else "green"
    ctx.log("info", f"[bold {cor}][3] Concluído.[/] {n_erros[0]} erros. "
                    f"→ texto_classificacao ({contagens.get('texto_classificacao', 0)} textos), "
                    f"item_categoria (+{n_recomputadas})")

    return StepResult(
        processed=n_ok[0], erros=n_erros[0],
        metrics={"textos_unicos": len(tarefas), "itens_afetados": n_itens,
                  "item_categoria_recomputadas": n_recomputadas,
                  "dedup": f"{n_itens_pend / max(n_textos, 1):.1f}x"},
        preview=[{"descricao": g["descricao"][:200], "itens": g["n_itens"]}
                 for g in tarefas[:30]],
    )
