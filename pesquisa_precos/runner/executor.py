"""
Orquestração de uma execução de etapa (Fase 3) — o que decide QUEM sobe o subprocesso, com
QUAIS parâmetros e sob QUAL lock. O que roda DENTRO do subprocesso é `runner.processo`.

Chamado pela CLI (comandos `run *`) e, nas fases seguintes, pela API — é o único caminho de
"dar play numa etapa via banco" (ADR-002: o processo web nunca executa a etapa na própria
thread, sempre sobe um subprocesso).

Fluxo de `tocar()`:
  1. `recuperar_travados()` — antes de mais nada, devolve à fila qualquer `run_etapa`
     `executando` cuja lease expirou (processo anterior morto sem avisar).
  2. `preparar()` — resolve os parâmetros pelas camadas de docs/03_ETAPAS.md §3
     (default do Pydantic ← `config_valor` da `config_versao` do run ← override do play) e
     grava `params_efetivos` ANTES de rodar, para que `retomar` nunca precise recalcular
     (ADR-008).
  3. `iniciar_subprocesso()` — adquire o lock (linha `execucao_lock`; o `pg_advisory_lock`
     complementar é tomado dentro do próprio subprocesso, ver `runner.lock`), marca
     `run_etapa.status='executando'` e sobe `python -m pesquisa_precos.runner.processo
     <run_etapa_id>` (ADR-002: subprocesso, não thread — etapas pesadas seguram o GIL).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo
from pesquisa_precos.etapas import registry
from pesquisa_precos.runner import lock


def recuperar_travados(timeout_s: int = lock.LEASE_PADRAO_S) -> list[int]:
    """Devolve à fila `run_etapa` presas (heartbeat parado há mais que `timeout_s`). Chamar
    antes de qualquer tentativa de lock — é o que garante que uma máquina reiniciada no meio
    de uma etapa não deixe o sistema "trancado" para sempre esperando um heartbeat que não
    vem mais (docs/04_FASES.md §Fase 3 item 3)."""
    recuperados: list[int] = []
    with db.sessao() as sessao:
        for linha in repo.leases_expiradas(sessao, timeout_s):
            repo.liberar_lease_expirada(sessao, linha["id"])
            recuperados.append(linha["id"])
    return recuperados


def preparar(run_id: int, chave: str, *,
            params_override: dict[str, Any] | None = None) -> int:
    """Resolve `Params` pelas três camadas e grava `params_efetivos`/`params_override` no
    `run_etapa` (criando-o se for a primeira vez). Devolve o `run_etapa_id`. Não inicia nada —
    é seguro chamar de novo (idempotente: reaproveita a linha existente)."""
    override = params_override or {}
    definicao = registry.obter(chave)
    with db.sessao() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            raise ValueError(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        config_valores = repo.ler_config(sessao, run["config_versao_id"])
        defaults = definicao.params_model().model_dump()
        # só entram valores de config que a etapa de fato declara em `Params` — config_valor
        # é compartilhado entre todas as etapas (thresholds, min_itens, top_n, ...).
        camada_config = {k: v for k, v in config_valores.items() if k in defaults}
        params_efetivos = definicao.params_model(**{**defaults, **camada_config, **override}) \
            .model_dump()
        repo.gravar_params(sessao, run_etapa_id, params_efetivos=params_efetivos,
                           params_override=override)
    return run_etapa_id


def iniciar_subprocesso(run_etapa_id: int, *, acao: str = "atualizar",
                        lease_timeout_s: int = lock.LEASE_PADRAO_S) -> subprocess.Popen:
    """Adquire o lock e sobe `runner.processo` como subprocesso independente. Levanta
    `lock.LockOcupado` se já existe uma execução em andamento (lease ainda válida) — o
    chamador (CLI/API) decide o que fazer com isso, não este módulo."""
    with db.sessao() as sessao:
        if repo.run_etapa_por_id(sessao, run_etapa_id) is None:
            raise ValueError(f"run_etapa {run_etapa_id} não existe — chame preparar() primeiro")
        if not lock.tentar_adquirir(sessao, run_etapa_id, pid=0, timeout_s=lease_timeout_s):
            detentor = lock.quem_detem(sessao)
            raise lock.LockOcupado(
                f"já existe uma execução em andamento (run_etapa={detentor}) — "
                f"cancele-a ou aguarde a lease expirar")
        repo.marcar_executando(sessao, run_etapa_id, acao=acao, pid=0)

    processo = subprocess.Popen(
        [sys.executable, "-m", "pesquisa_precos.runner.processo", str(run_etapa_id)])

    with db.sessao() as sessao:
        repo.heartbeat(sessao, run_etapa_id, processo.pid)
        lock.renovar(sessao, run_etapa_id, timeout_s=lease_timeout_s)
    return processo


def tocar(run_id: int, chave: str, *, acao: str = "atualizar",
         params_override: dict[str, Any] | None = None,
         lease_timeout_s: int = lock.LEASE_PADRAO_S) -> tuple[int, subprocess.Popen]:
    """Atalho: recupera travados, prepara e inicia. Devolve `(run_etapa_id, processo)` — o
    chamador decide se espera (`processo.wait()`) ou deixa rodando em background."""
    recuperar_travados(lease_timeout_s)
    run_etapa_id = preparar(run_id, chave, params_override=params_override)
    processo = iniciar_subprocesso(run_etapa_id, acao=acao, lease_timeout_s=lease_timeout_s)
    return run_etapa_id, processo


def cancelar(run_etapa_id: int) -> bool:
    """`UPDATE` puro (ADR-005) — o subprocesso em execução é quem observa isto, no próprio
    `ctx.cancelado()`. Devolve `False` se a etapa não estava `executando` (nada a cancelar)."""
    with db.sessao() as sessao:
        return repo.solicitar_cancelamento(sessao, run_etapa_id)


def status(run_etapa_id: int) -> dict[str, Any] | None:
    with db.sessao() as sessao:
        return repo.run_etapa_por_id(sessao, run_etapa_id)
