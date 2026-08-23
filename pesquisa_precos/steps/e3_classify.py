"""
Etapa 3 — Classifica a categoria de cada item do PNCP no LLM (multi-label).

A descrição do PNCP se repete muito: o mesmo texto reaparece milhares de vezes. Classificamos
cada par (descrição, unidade) distinto uma vez e espalhamos o rótulo para todos os `item_key`
iguais, o que derruba as chamadas de LLM de O(itens) para O(textos distintos). A saída
continua por `item_key`, com a referência à ata ou contrato intacta. Item sem categoria de
conteúdo morre aqui.

Não classificar por item em vez de por texto distinto: o dedup de ~5x é o que segura o custo
desta etapa, a mais cara do ciclo.
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
# um texto já pago nunca mais volta ao modelo. No CSV, o agrupamento era refeito a cada
# execução sobre 1,6 milhão de linhas em memória; aqui o `texto_hash` já veio calculado da
# ingestão da etapa 2 e o agrupamento é do banco.

class _Acumulador:
    """Contadores e lote pendente da classificação, compartilhados entre as threads.

    Substitui as listas de um elemento (`n_ok = [0]`) que serviam de célula mutável. O lock
    protege tanto os contadores quanto o lote, que é gravado quando enche.
    """

    TAMANHO_LOTE = 500

    def __init__(self, db, repo):
        self._db = db
        self._repo = repo
        self._lock = threading.Lock()
        self.ok = 0
        self.erros = 0
        self.lote: list[tuple] = []

    def registra_erro(self) -> None:
        with self._lock:
            self.erros += 1

    def registra_ok(self, linha: tuple) -> None:
        """Guarda a linha classificada; grava quando o lote enche."""
        with self._lock:
            self.ok += 1
            self.lote.append(linha)
            cheio = len(self.lote) >= self.TAMANHO_LOTE
        if cheio:
            self.flush()

    def flush(self) -> None:
        """Grava o lote acumulado. Em lote, e não por texto: uma transação por item seria
        mais lenta que a própria chamada de LLM que estamos economizando."""
        with self._lock:
            if not self.lote:
                return
            pendente, self.lote = self.lote, []
        with self._db.raw_connection() as conn:
            self._repo.gravar(conn, pendente)
            conn.commit()


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
    ctx.log("info", f"[bold][3] Dedup: {n_itens_pend} itens -> {n_textos} textos únicos[/]"
                    f"{limite_txt} · classificando {len(tarefas)} textos "
                    f"({n_itens} itens), concorrência: {params.concurrency}")

    # Um Curador por thread: compartilhar um cliente HTTP serializaria as chamadas.
    local = threading.local()

    def curador():
        if not hasattr(local, "instancia"):
            local.instancia = ctx.providers.novo_chat(
                curador_kwargs={"prompts_ativos": prompts_ativos, **reasoning_kw}).curador
        return local.instancia

    nome_provedor = resolucao_chat.info.name
    model = getattr(resolucao_chat.info, "model", None) or nome_provedor
    acumulador = _Acumulador(db, repo)

    def classificar(grupo):
        return curador().classificar_categoria(grupo["descricao"], grupo.get("unidade") or "")

    def ao_classificar(grupo, res):
        conf = res.get("confianca", "")
        if conf == "erro":
            # Texto com erro não entra na tabela: entrar o marcaria como pago sem ter sido
            # classificado, e o retry nunca mais o encontraria.
            acumulador.registra_erro()
            ctx.item_error(grupo["texto_hash"], res.get("_erro"), name=grupo["descricao"])
            return
        # `confianca` é `real` no banco e palavra no LLM; a escala ordinal é declarada em
        # `repo.CONFIANCA_ORDINAL`, a mesma que a migração usa.
        acumulador.registra_ok((
            grupo["texto_hash"], grupo["descricao"], grupo.get("unidade"),
            res["categorias"], repo.confianca_para_real(conf),
            res.get("_prompt_versao_id"), model, nome_provedor, None))

    def ao_falhar(grupo, exc):
        acumulador.registra_erro()
        ctx.item_error(grupo["texto_hash"], exc, name=grupo["descricao"])

    ctx.progresso(0, len(tarefas), descricao="classificando")
    try:
        executar_paralelo(tarefas, classificar, concurrency=params.concurrency,
                          on_result=ao_classificar, on_error=ao_falhar,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    finally:
        acumulador.flush()   # o que já foi pago é gravado mesmo se a etapa cair no meio

    with db.session() as s:
        n_recomputadas = repo.recomputar_item_categoria(s)
        s.commit()
        contagens = repo.contar(s)

    cor = "yellow" if acumulador.erros else "green"
    ctx.log("info", f"[bold {cor}][3] Concluído.[/] {acumulador.erros} erros. "
                    f"-> texto_classificacao ({contagens.get('texto_classificacao', 0)} textos), "
                    f"item_categoria (+{n_recomputadas})")

    return StepResult(
        processed=acumulador.ok, errors=acumulador.erros,
        metrics={"textos_unicos": len(tarefas), "itens_afetados": n_itens,
                 "item_categoria_recomputadas": n_recomputadas,
                 "dedup": f"{n_itens_pend / max(n_textos, 1):.1f}x"},
        preview=[{"descricao": g["descricao"][:200], "itens": g["n_itens"]}
                 for g in tarefas[:30]],
    )
