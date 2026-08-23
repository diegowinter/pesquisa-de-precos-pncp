"""
`RunContext` de banco (Fase 3) — o que `runner.worker` injeta num subprocesso.

Onde o `ContextoConsole` (Fase 1) fala com `rich.Progress` e um arquivo de errors, este fala
com `run_step`, `run_log`, `item_error` e o lock — e é isso, não a etapa, que muda: o corpo de
`executar()` de cada step continua igual, porque os dois implementam o mesmo `Protocol`
(docs/03_ETAPAS.md §1).

Duas sessões, de propósito:
  - `db` é a sessão que a ETAPA usa para o próprio domínio (grava `texto_classificacao`, etc).
    É a que o `with ctx.db.begin(): ...` das etapas abre e fecha (docs/08_CONVENCOES.md §5.3).
  - `_sessao_execucao` é uma sessão SEPARADA, só para a contabilidade do runner (progresso,
    log, heartbeat, custo, lock). Compartilhar a mesma sessão faria `ctx.progresso()` ou
    `ctx.cancelado()` no meio de uma transação de domínio da etapa fazer commit/rollback de
    coisas que não são dela — e é justamente a conexão desta segunda sessão que carrega o
    `pg_advisory_lock` (ver `runner.lock`), então ela precisa ficar viva o tempo todo, com vida
    própria, independente do que a etapa fizer com `db`.

`cancelado()` não bate no banco a cada chamada (seria uma consulta por item nalgumas etapas):
reaproveita o mesmo intervalo do heartbeat — no máximo `min(30s, lease/3)` de atraso para
perceber um cancelamento, o que é aceitável frente ao custo de uma SELECT por item.
"""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy.orm import Session

from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps.base import Acao, Modo, TetoDeCustoExcedido
from pesquisa_precos.providers.resolver import Providers
from pesquisa_precos.runner import lock


class DbContext:
    """Ver `pesquisa_precos.steps.base.RunContext` para o contrato."""

    def __init__(self, db: Session, *, sessao_execucao: Session, run_id: int,
                run_etapa_id: int, step: str, action: Acao, mode: Modo,
                cost_cap_usd: float | None = None, lease_timeout_s: int = lock.LEASE_PADRAO_S):
        self.db = db
        self.run_id = run_id
        self.run_etapa_id = run_etapa_id
        self.step = step
        self.action = action
        self.mode = mode
        # Sessão de DOMÍNIO da etapa (`self.db`), não a de execução — resolver via
        # `provider_capability` é leitura, cabe na mesma sessão que a etapa já usa (Fase 7).
        self.providers = Providers(self.db)

        self._sessao_execucao = sessao_execucao
        self._teto_custo_usd = cost_cap_usd
        self._lease_timeout_s = lease_timeout_s
        self._intervalo_heartbeat_s = min(30, max(5, lease_timeout_s / 3))
        self._ultimo_heartbeat = 0.0
        self._cancelado_cache = False

    # ── contrato RunContext ────────────────────────────────────────────────
    def progresso(self, processed: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        repo.atualizar_progresso(self._sessao_execucao, self.run_etapa_id, processed, total)
        self._sessao_execucao.commit()
        self._heartbeat()

    def log(self, nivel: str, msg: str, **contexto: Any) -> None:
        repo.registrar_log(self._sessao_execucao, self.run_id, self.step, nivel, msg,
                           contexto or None)
        self._sessao_execucao.commit()

    def item_error(self, key: str, exc: object, *, tipo: str = "", name: str = "") -> None:
        repo.registrar_erro_item(
            self._sessao_execucao, self.run_id, self.step, key,
            tipo or type(exc).__name__, name or str(exc))
        self._sessao_execucao.commit()

    def cancelado(self) -> bool:
        self._heartbeat()
        return self._cancelado_cache

    def gastar(self, usd: float) -> None:
        if usd <= 0:
            return
        total_run = repo.incrementar_custo(self._sessao_execucao, self.run_etapa_id,
                                           self.run_id, usd)
        self._sessao_execucao.commit()
        if self._teto_custo_usd is not None and float(total_run) > self._teto_custo_usd:
            raise TetoDeCustoExcedido(
                f"teto de US$ {self._teto_custo_usd:.2f} do run {self.run_id} excedido na "
                f"step {self.step} (gasto acumulado US$ {float(total_run):.2f})")

    # ── mecânica interna ─────────────────────────────────────────────────────────
    def _heartbeat(self, forcar: bool = False) -> None:
        agora = time.monotonic()
        if not forcar and (agora - self._ultimo_heartbeat) < self._intervalo_heartbeat_s:
            return
        self._ultimo_heartbeat = agora
        repo.heartbeat(self._sessao_execucao, self.run_etapa_id, os.getpid())
        lock.renovar(self._sessao_execucao, self.run_etapa_id, timeout_s=self._lease_timeout_s)
        status = repo.status_run_etapa(self._sessao_execucao, self.run_etapa_id)
        self._sessao_execucao.commit()
        self._cancelado_cache = status == "cancelled"
