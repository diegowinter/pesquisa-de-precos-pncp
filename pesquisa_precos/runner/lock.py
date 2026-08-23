"""
Lock de execução única (ADR-001: single-writer) — linha + `pg_advisory_lock`, com lease.

Duas camadas, deliberadamente redundantes:

  1. **Linha `run_lock`** (`id=1`, PK fixa): o mecanismo de verdade. `tentar_adquirir` é
     um `UPDATE ... ON CONFLICT ... WHERE expires_at < now()` atômico — só rouba o lock de quem
     já expirou. Sobrevive a reinício do banco e é o que `runner.launcher` consulta para saber
     "tem etapa rodando?" sem precisar de uma conexão viva.
  2. **`pg_advisory_lock`** na conexão que fica aberta pelo `processo.py` durante a etapa
     inteira: é o cinto e suspensórios que "some sozinho se a conexão cair" (docs/04_FASES.md
     §Fase 3 item 2) — se o processo morre sem conseguir atualizar a linha (kill -9, queda de
     energia), o advisory lock cai junto quando o Postgres fecha a conexão morta. A linha por
     si só não teria esse comportamento: ficaria "presa" até a lease expirar.

A lease (`expires_at`) é quem resolve o caso em que nem a conexão caiu de forma limpa: se o
heartbeat parar de renovar (`renovar`), depois de `timeout_s` o lock conta como livre para
`tentar_adquirir`, e `execucao.leases_expiradas` devolve a etapa à fila (docs/04_FASES.md §Fase
3 item 3).
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

# Única lock global do sistema — ADR-001 (um processo, um banco, um operador; não há
# concorrência entre execuções a serializar além desta).
CHAVE_ADVISORY = 727_272_727
LEASE_PADRAO_S = 300


class LockOcupado(RuntimeError):
    """Já existe uma etapa em execução (lease ainda não expirou)."""


def tentar_adquirir(sessao: Session, run_etapa_id: int, pid: int, *,
                    timeout_s: int = LEASE_PADRAO_S) -> bool:
    """Atômico: só grava se a linha estiver livre (`run_etapa_id IS NULL`, nunca usada) ou se
    a lease anterior já expirou. Devolve `True` se adquiriu."""
    linha = sessao.execute(
        text("INSERT INTO run_lock (id, run_etapa_id, pid, acquired_at, expires_at) "
             "VALUES (1, :re, :pid, now(), now() + make_interval(secs => :t)) "
             "ON CONFLICT (id) DO UPDATE SET "
             "  run_etapa_id = EXCLUDED.run_etapa_id, pid = EXCLUDED.pid, "
             "  acquired_at = EXCLUDED.acquired_at, expires_at = EXCLUDED.expires_at "
             "WHERE run_lock.run_etapa_id IS NULL "
             "   OR run_lock.expires_at IS NULL "
             "   OR run_lock.expires_at < now() "
             "RETURNING run_etapa_id"),
        {"re": run_etapa_id, "pid": pid, "t": timeout_s}).first()
    return linha is not None


def renovar(sessao: Session, run_etapa_id: int, *, timeout_s: int = LEASE_PADRAO_S) -> None:
    """Chamado a cada heartbeat (`contexto_banco.DbContext._heartbeat`). Só renova se o
    lock ainda for desta execução — se outro processo já o tomou (lease anterior expirou e
    outra etapa começou), não há nada a renovar."""
    sessao.execute(
        text("UPDATE run_lock SET expires_at = now() + make_interval(secs => :t) "
             "WHERE id = 1 AND run_etapa_id = :re"), {"re": run_etapa_id, "t": timeout_s})


def liberar(sessao: Session, run_etapa_id: int) -> None:
    """Fim limpo da etapa (concluída, falhou tratado, cancelada). Libera só se o lock ainda é
    desta execução — não rouba o lock de quem já o tomou depois de uma lease expirada."""
    sessao.execute(
        text("UPDATE run_lock SET run_etapa_id = NULL, pid = NULL, "
             "                          acquired_at = NULL, expires_at = NULL "
             "WHERE id = 1 AND run_etapa_id = :re"), {"re": run_etapa_id})


def quem_detem(sessao: Session) -> dict | None:
    """`None` = livre. Usado pelo CLI/API para recusar um novo play com mensagem clara em vez
    de deixar o segundo processo brigar pela linha e falhar tarde."""
    linha = sessao.execute(
        text("SELECT run_etapa_id, pid, acquired_at, expires_at FROM run_lock "
             "WHERE id = 1 AND run_etapa_id IS NOT NULL")).mappings().first()
    return dict(linha) if linha else None


def advisory_tentar(sessao: Session) -> bool:
    """`pg_try_advisory_lock` na conexão ATUAL da sessão — não bloqueia. Deve ser chamado na
    mesma sessão/conexão que fica aberta pelo resto da execução (`processo.py`), nunca numa
    sessão de vida curta: o lock desaparece quando a conexão fecha, que é exatamente o
    comportamento desejado, mas só funciona se for a conexão certa."""
    return bool(sessao.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": CHAVE_ADVISORY}).scalar())


def advisory_liberar(sessao: Session) -> None:
    sessao.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CHAVE_ADVISORY})
