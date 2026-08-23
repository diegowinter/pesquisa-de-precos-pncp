"""
`RunContext` de banco (Fase 3) — o que `runner.worker` injeta num subprocesso.

Onde o `ContextoConsole` (Fase 1) fala com `rich.Progress` e um arquivo de errors, este fala
com `run_step`, `run_log`, `item_error` e o lock — e é isso, não a etapa, que muda: o corpo de
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
reaproveita o cache que a thread de heartbeat atualiza — no máximo um intervalo de atraso
para perceber um cancelamento, o que é aceitável frente ao custo de uma SELECT por item.

O heartbeat roda em THREAD PRÓPRIA, com sessão própria (2026-08-23). Antes ele pegava carona
em `progresso()`/`cancelado()`, o que confundia duas perguntas diferentes: "a etapa avançou?"
e "o processo está vivo?". A etapa 2 mostrou a diferença na prática — 12 minutos dentro de uma
única busca do PNCP, sem chamar nenhum dos dois, com a lease de 300 s vencendo e o supervisor
pronto para matar como zumbi uma execução que estava coletando normalmente.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps.base import MANTER, Acao, Modo, TetoDeCustoExcedido
from pesquisa_precos.providers.resolver import Providers
from pesquisa_precos.runner import lock


_MARKUP_RICH = re.compile(r"\[/?[a-z0-9 #]*\]")


def _sem_markup(texto: str) -> str:
    """As etapas escrevem para o `rich` (`[cyan]capacete[/]`); a tela é HTML."""
    return _MARKUP_RICH.sub("", texto).strip()


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
        self._sub = {"processed": 0, "total": None, "descricao": ""}
        self._ultimo_sub = 0.0
        self._parar = threading.Event()
        self._thread_heartbeat: threading.Thread | None = None

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

    def subprogresso(self, processed: int | None = None, total: Any = MANTER,
                     descricao: str | None = None) -> None:
        """Barra secundária: onde a etapa está DENTRO da unidade principal. Sem ela, uma busca
        da etapa 2 com 3 mil documentos fica meia hora marcando "0 / 174" na tela.

        Cada argumento omitido preserva o valor anterior — a etapa chama isto três vezes por
        motivos diferentes (nova unidade, total descoberto, mais um item)."""
        if processed is not None:
            self._sub["processed"] = processed
        if total is not MANTER:
            self._sub["total"] = total
        if descricao is not None:
            self._sub["descricao"] = _sem_markup(descricao)
        # Limite de taxa: isto é chamado uma vez por documento, e a barra não fica melhor com
        # mais de uma escrita por segundo.
        agora = time.monotonic()
        if agora - self._ultimo_sub < 1.0:
            return
        self._ultimo_sub = agora
        repo.atualizar_subprogresso(self._sessao_execucao, self.run_etapa_id,
                                    self._sub["processed"], self._sub["total"],
                                    self._sub["descricao"])
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
    def iniciar_heartbeat(self) -> None:
        """Sobe a thread que bate o heartbeat enquanto a etapa roda. Chamada pelo worker."""
        if self._thread_heartbeat is not None:
            return
        self._thread_heartbeat = threading.Thread(
            target=self._laco_heartbeat, name="heartbeat", daemon=True)
        self._thread_heartbeat.start()

    def encerrar(self) -> None:
        self._parar.set()
        if self._thread_heartbeat is not None:
            self._thread_heartbeat.join(timeout=5)
            self._thread_heartbeat = None

    def _laco_heartbeat(self) -> None:
        """Sessão PRÓPRIA: `Session` do SQLAlchemy não é thread-safe, e a sessão de execução
        é justamente a que carrega o `pg_advisory_lock` — dividi-la com outra thread
        corromperia o estado de transação da etapa. `renovar()` e `heartbeat()` são UPDATEs
        simples, indiferentes a qual conexão os emite."""
        from pesquisa_precos.db import session as db

        sessao = db.create_session()
        try:
            while not self._parar.wait(self._intervalo_heartbeat_s):
                try:
                    self._bater(sessao)
                except Exception:  # noqa: BLE001 — heartbeat não derruba a etapa
                    # Um blip no banco não pode matar a coleta: a lease é a rede de baixo,
                    # e ela ainda tem `lease_timeout_s` de folga até vencer.
                    sessao.rollback()
        finally:
            sessao.close()

    def _bater(self, sessao: Session) -> None:
        status = repo.status_run_etapa(sessao, self.run_etapa_id)
        self._cancelado_cache = status == "cancelled"
        repo.heartbeat(sessao, self.run_etapa_id, os.getpid())
        # Etapa cancelada NÃO renova a lease. A etapa sai quando chegar ao próximo ponto de
        # checagem (o cancelamento é cooperativo), mas até lá o lock precisa poder expirar —
        # senão um worker que ninguém mais quer trava o sistema inteiro, que foi o que
        # aconteceu em 2026-08-23: run abortado, etapa cancelada, e a lease sendo empurrada
        # de 30 em 30 segundos por um processo condenado.
        if not self._cancelado_cache:
            lock.renovar(sessao, self.run_etapa_id, timeout_s=self._lease_timeout_s)
        sessao.commit()
        self._ultimo_heartbeat = time.monotonic()

    def _heartbeat(self, forcar: bool = False) -> None:
        """Caminho antigo, mantido para quem não sobe a thread (testes e `NullContext`-likes):
        só bate se a thread não estiver cuidando disso."""
        if self._thread_heartbeat is not None:
            return
        agora = time.monotonic()
        if not forcar and (agora - self._ultimo_heartbeat) < self._intervalo_heartbeat_s:
            return
        self._bater(self._sessao_execucao)
