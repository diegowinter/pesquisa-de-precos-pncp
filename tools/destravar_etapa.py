"""Diagnostica — e, se voce mandar, encerra — o worker que ficou pendurado segurando o lock.

O cancelamento e COOPERATIVO por desenho (ADR-005): a web so marca `status='cancelled'` e o
worker sai quando percebe, entre uma unidade de trabalho e outra. Se ele estiver preso numa
chamada HTTP longa, ou nao cooperar, o processo continua vivo com o `pg_advisory_lock` na mao
e NENHUMA nova execucao sobe — a tela mostra "0 / N" parado para sempre.

Foi o que aconteceu em 2026-08-30: o worker das 13:27 sobreviveu ao cancelamento, e destravar
exigiu listar processos do sistema operacional na mao.

    uv run python tools/destravar_etapa.py             # so diagnostica
    uv run python tools/destravar_etapa.py --encerrar   # mata o PID do lock

Encerrar NAO perde trabalho: a etapa 5 grava cada documento assim que ele termina.
"""
import os
import signal
import sys

from sqlalchemy import text

from pesquisa_precos.db import session as db

SQL = """
SELECT l.run_etapa_id, l.pid AS pid_lock, l.expires_at, now() > l.expires_at AS lease_vencida,
       r.step, r.status, r.pid AS pid_step, r.processed, r.total,
       now() - r.heartbeat_at AS heartbeat_parado_ha
  FROM run_lock l LEFT JOIN run_step r ON r.id = l.run_etapa_id
 WHERE l.id = 1 AND l.run_etapa_id IS NOT NULL
"""

ADVISORY = """
SELECT a.pid, a.state, now() - a.state_change AS parado_ha
  FROM pg_locks l JOIN pg_stat_activity a USING (pid)
 WHERE l.locktype = 'advisory'
"""


def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def main() -> None:
    with db.session() as s:
        lock = s.execute(text(SQL)).mappings().first()
        advisory = s.execute(text(ADVISORY)).mappings().all()

    if not lock:
        print("lock livre — nenhuma etapa detém o run_lock")
    else:
        for k, v in lock.items():
            print(f"{k:22}: {v}")
    print(f"\nconexoes com advisory lock: {len(advisory)}")
    for a in advisory:
        print(f"  pid {a['pid']} · {a['state']} · parado ha {a['parado_ha']}")

    # `run_lock.pid` so passou a ser preenchido em 2026-08-30; `run_step.pid` e o historico.
    pid = (lock or {}).get("pid_lock") or (lock or {}).get("pid_step") or 0
    if not pid:
        print("\nsem PID conhecido — nada a encerrar")
        return
    print(f"\nPID do worker: {pid} ({'vivo' if _vivo(pid) else 'ja morto'})")

    if "--encerrar" not in sys.argv:
        print("(nada encerrado — rode com --encerrar)")
        return
    if not _vivo(pid):
        print("processo ja nao existe")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"SIGTERM enviado para {pid}. Confira com este mesmo comando; o lease do run_lock "
          f"expira sozinho e o proximo play chama recuperar_travados().")


if __name__ == "__main__":
    main()
