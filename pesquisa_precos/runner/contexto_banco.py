"""
`ContextoExecucao` de banco (Fase 3) — o que `runner.processo` injeta num subprocesso.

Onde o `ContextoConsole` (Fase 1) fala com `rich.Progress` e um arquivo de erros, este fala
com `run_etapa`, `run_log`, `erro_item` e o lock — e é isso, não a etapa, que muda: o corpo de
`executar()` de cada etapa continua igual, porque os dois implementam o mesmo `Protocol`
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

from pesquisa_precos.db.repos import execucao as repo
from pesquisa_precos.etapas.base import Acao, Modo, TetoDeCustoExcedido
from pesquisa_precos.providers.resolver import Provedores
from pesquisa_precos.runner import lock


class ContextoBanco:
    """Ver `pesquisa_precos.etapas.base.ContextoExecucao` para o contrato."""

    def __init__(self, db: Session, *, sessao_execucao: Session, run_id: int,
                run_etapa_id: int, etapa: str, acao: Acao, modo: Modo, config: dict,
                teto_custo_usd: float | None = None, lease_timeout_s: int = lock.LEASE_PADRAO_S):
        self.db = db
        self.run_id = run_id
        self.run_etapa_id = run_etapa_id
        self.etapa = etapa
        self.acao = acao
        self.modo = modo
        self.config = config
        # Sessão de DOMÍNIO da etapa (`self.db`), não a de execução — resolver via
        # `capacidade_provedor` é leitura, cabe na mesma sessão que a etapa já usa (Fase 7).
        self.provedores = Provedores(self.config, self.db)

        self._sessao_execucao = sessao_execucao
        self._teto_custo_usd = teto_custo_usd
        self._lease_timeout_s = lease_timeout_s
        self._intervalo_heartbeat_s = min(30, max(5, lease_timeout_s / 3))
        self._ultimo_heartbeat = 0.0
        self._cancelado_cache = False

    # ── contrato ContextoExecucao ────────────────────────────────────────────────
    def progresso(self, processados: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        repo.atualizar_progresso(self._sessao_execucao, self.run_etapa_id, processados, total)
        self._sessao_execucao.commit()
        self._heartbeat()

    def log(self, nivel: str, msg: str, **contexto: Any) -> None:
        repo.registrar_log(self._sessao_execucao, self.run_id, self.etapa, nivel, msg,
                           contexto or None)
        self._sessao_execucao.commit()

    def erro_item(self, chave: str, exc: object, *, tipo: str = "", nome: str = "") -> None:
        repo.registrar_erro_item(
            self._sessao_execucao, self.run_id, self.etapa, chave,
            tipo or type(exc).__name__, nome or str(exc))
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
                f"etapa {self.etapa} (gasto acumulado US$ {float(total_run):.2f})")

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
        self._cancelado_cache = status == "cancelada"
