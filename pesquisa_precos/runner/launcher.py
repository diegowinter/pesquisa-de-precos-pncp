"""
Orquestração de uma execução de step (Fase 3) — o que decide QUEM sobe o subprocesso, com
QUAIS parâmetros e sob QUAL lock. O que roda DENTRO do subprocesso é `runner.worker`.

Chamado pela CLI (comandos `run *`) e, nas fases seguintes, pela API — é o único caminho de
"dar play numa step via banco" (ADR-002: o processo web nunca executa a etapa na própria
thread, sempre sobe um subprocesso).

Fluxo de `tocar()`:
  1. `recuperar_travados()` — antes de mais nada, devolve à fila qualquer `run_step`
     `executando` cuja lease expirou (processo anterior morto sem avisar).
  2. `preparar()` — resolve os parâmetros pelas camadas de docs/03_ETAPAS.md §3
     (default do Pydantic ← `config_value` da `config_version` do run ← override do play) e
     grava `effective_params` ANTES de rodar, para que `retomar` nunca precise recalcular
     (ADR-008).
  3. `iniciar_subprocesso()` — adquire o lock (linha `run_lock`; o `pg_advisory_lock`
     complementar é tomado dentro do próprio subprocesso, ver `runner.lock`), marca
     `run_step.status='running'` e sobe `python -m pesquisa_precos.runner.worker
     <run_etapa_id>` (ADR-002: subprocesso, não thread — etapas pesadas seguram o GIL).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps import registry
from pesquisa_precos.providers import health
from pesquisa_precos.runner import lock


class ProvedorIndisponivel(RuntimeError):
    """Health check pré-play (docs/04_FASES.md §Fase 7 item 6) achou um provedor fora do ar
    para uma capacidade que a etapa precisa. Levantado ANTES de subir o subprocesso — é a
    diferença entre saber agora e descobrir 40 minutos depois, no meio da 6a."""


def recuperar_travados(timeout_s: int = lock.LEASE_PADRAO_S) -> list[int]:
    """Devolve à fila `run_step` presas (heartbeat parado há mais que `timeout_s`). Chamar
    antes de qualquer tentativa de lock — é o que garante que uma máquina reiniciada no meio
    de uma etapa não deixe o sistema "trancado" para sempre esperando um heartbeat que não
    vem mais (docs/04_FASES.md §Fase 3 item 3)."""
    recuperados: list[int] = []
    with db.session() as sessao:
        for linha in repo.leases_expiradas(sessao, timeout_s):
            repo.liberar_lease_expirada(sessao, linha["id"])
            recuperados.append(linha["id"])
    return recuperados


def preparar(run_id: int, key: str, *,
            params_override: dict[str, Any] | None = None) -> int:
    """Resolve `Params` pelas três camadas e grava `effective_params`/`params_override` no
    `run_step` (criando-o se for a primeira vez). Devolve o `run_etapa_id`. Não inicia nada —
    é seguro chamar de novo (idempotente: reaproveita a linha existente)."""
    override = params_override or {}
    definicao = registry.obter(key)
    with db.session() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            raise ValueError(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        config_valores = repo.ler_config(sessao, run["config_version_id"])
        defaults = definicao.params_model().model_dump()
        # só entram valores de config que a etapa de fato declara em `Params` — config_value
        # é compartilhado entre todas as etapas (thresholds, min_itens, top_n, ...).
        camada_config = {k: v for k, v in config_valores.items() if k in defaults}
        effective_params = definicao.params_model(**{**defaults, **camada_config, **override}) \
            .model_dump()
        repo.gravar_params(sessao, run_etapa_id, effective_params=effective_params,
                           params_override=override)
    return run_etapa_id


def checar_saude_previa(key: str) -> list[dict]:
    """Sonda as capacidades que a etapa declara no registry (Fase 7) ANTES do play. Só sonda
    capabilities que têm linha em `provider_capability` — enquanto o banco de provedores está
    vazio (estado de hoje, ver CLAUDE.md), a etapa resolve pelo `.env` como sempre resolveu, e
    esta checagem fica muda: ela é uma feature de quem já configurou provider pela interface,
    não um bloqueio novo para quem nunca usou.

    Devolve a lista de sondagens (mesmo formato de `providers.health.checar_capacidade`).
    """
    definicao = registry.obter(key)
    if not definicao.capabilities:
        return []
    with db.session() as sessao:
        configuradas = [c for c in definicao.capabilities
                        if repo.capacidade_provedor_info(sessao, c) is not None]
        if not configuradas:
            return []
        return health.checar_capabilities(configuradas, sessao=sessao)


def iniciar_subprocesso(run_etapa_id: int, *, acao: str = "update",
                        lease_timeout_s: int = lock.LEASE_PADRAO_S,
                        pular_checagem_saude: bool = False) -> subprocess.Popen:
    """Adquire o lock e sobe `runner.worker` como subprocesso independente. Levanta
    `lock.LockOcupado` se já existe uma execução em andamento (lease ainda válida) — o
    chamador (CLI/API) decide o que fazer com isso, não este módulo.

    Antes de subir o subprocesso, sonda a saúde dos provedores que a etapa vai usar
    (`checar_saude_previa`) e levanta `ProvedorIndisponivel` com uma mensagem clara se algum
    estiver fora do ar — em vez de deixar a etapa começar e falhar no meio (Fase 7, critério de
    aceite "health check detecta o túnel caído antes do play, não 40 minutos depois").
    """
    if not pular_checagem_saude:
        with db.session() as sessao_lookup:
            run_step = repo.run_etapa_por_id(sessao_lookup, run_etapa_id)
        if run_step is not None:
            indisponiveis = [r for r in checar_saude_previa(run_step["step"])
                             if not r["healthy"]]
            if indisponiveis:
                detalhe = "; ".join(f"{r['capability']}/{r['provider']}: {r['mensagem']}"
                                    for r in indisponiveis)
                raise ProvedorIndisponivel(
                    f"provider indisponível para a step {run_step['step']} — {detalhe}")

    with db.session() as sessao:
        if repo.run_etapa_por_id(sessao, run_etapa_id) is None:
            raise ValueError(f"run_step {run_etapa_id} não existe — chame preparar() primeiro")
        if not lock.tentar_adquirir(sessao, run_etapa_id, pid=0, timeout_s=lease_timeout_s):
            detentor = lock.quem_detem(sessao)
            raise lock.LockOcupado(
                f"já existe uma execução em andamento (run_step={detentor}) — "
                f"cancele-a ou aguarde a lease expirar")
        repo.marcar_executando(sessao, run_etapa_id, acao=acao, pid=0)

    processo = subprocess.Popen(
        [sys.executable, "-m", "pesquisa_precos.runner.worker", str(run_etapa_id)])

    with db.session() as sessao:
        repo.heartbeat(sessao, run_etapa_id, processo.pid)
        lock.renovar(sessao, run_etapa_id, timeout_s=lease_timeout_s)
    return processo


def tocar(run_id: int, key: str, *, acao: str = "update",
         params_override: dict[str, Any] | None = None,
         lease_timeout_s: int = lock.LEASE_PADRAO_S,
         pular_checagem_saude: bool = False) -> tuple[int, subprocess.Popen]:
    """Atalho: recupera travados, prepara e inicia. Devolve `(run_etapa_id, processo)` — o
    chamador decide se espera (`processo.wait()`) ou deixa rodando em background."""
    recuperar_travados(lease_timeout_s)
    run_etapa_id = preparar(run_id, key, params_override=params_override)
    processo = iniciar_subprocesso(run_etapa_id, acao=acao, lease_timeout_s=lease_timeout_s,
                                   pular_checagem_saude=pular_checagem_saude)
    return run_etapa_id, processo


def cancelar(run_etapa_id: int) -> bool:
    """`UPDATE` puro (ADR-005) — o subprocesso em execução é quem observa isto, no próprio
    `ctx.cancelado()`. Devolve `False` se a etapa não estava `executando` (nada a cancelar)."""
    with db.session() as sessao:
        return repo.solicitar_cancelamento(sessao, run_etapa_id)


def status(run_etapa_id: int) -> dict[str, Any] | None:
    with db.session() as sessao:
        return repo.run_etapa_por_id(sessao, run_etapa_id)
