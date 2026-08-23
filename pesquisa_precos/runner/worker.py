"""
Ponto de entrada do subprocesso de uma etapa (ADR-002) — o que `runner.launcher` sobe.

Uso: `python -m pesquisa_precos.runner.worker <run_etapa_id>`

O `run_etapa` já existe e já está `executando` quando este processo começa (é
`launcher.iniciar_subprocesso` quem faz isso, ANTES de subir o subprocesso — assim, se o
`Popen` falhar por qualquer razão do SO, o estado no banco continua consistente e não fica um
`run_etapa` fantasma sem processo nenhum tentando geri-lo).

Duas sessões abertas o processo inteiro, pelo motivo documentado em `contexto_banco`:
`sessao_execucao` carrega o `pg_advisory_lock` e é usada só pela contabilidade do runner;
`db_etapa` é a que a etapa recebe em `ctx.db` para o próprio domínio.

Código de saída: 0 = concluída (ou cancelada — não é falha), 1 = erro, 2 = teto de custo
excedido. Nenhum deles é usado por quem chama hoje (a CLI de imediato só espera o processo
morrer), mas existe para a Fase 4 (API), que vai querer diferenciar os três.
"""

import os
import sys
import traceback

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps import registry
from pesquisa_precos.steps.base import TetoDeCustoExcedido
from pesquisa_precos.runner import fingerprint, lock
from pesquisa_precos.runner.db_context import DbContext
from pesquisa_precos.services import notifications


def _notificar_best_effort(run_id: int, etapa: str, evento: str, detalhe: str = "") -> None:
    """Fase 9, item 3: dispara logo após o `commit` de estado (nunca antes — a notificação não
    pode fazer o `run_etapa` parecer concluído/falho antes de o banco de fato refletir isso).
    Envolvida numa exceção genérica: `notifications.notificar_evento` já é best-effort por
    dentro, mas esta é a segunda rede — nenhuma falha aqui pode propagar para o `except`
    principal e mascarar o motivo real de uma etapa ter falhado."""
    try:
        notifications.notificar_evento(run_id, etapa, evento, detalhe=detalhe)
    except Exception:  # noqa: BLE001 — ver docstring
        pass


def run(run_etapa_id: int) -> int:
    sessao_execucao = db.create_session()

    if not lock.advisory_tentar(sessao_execucao):
        # A linha `execucao_lock` já foi reservada por `executor` antes de este processo
        # subir; chegar aqui sem conseguir o advisory lock significa outro processo (mesmo
        # host Postgres) disputando a MESMA chave — não deveria acontecer sob uso normal, mas
        # falhar alto e claro é melhor que rodar sem a segunda trava.
        repo.marcar_falhou(sessao_execucao, run_etapa_id,
                           "não foi possível adquirir o pg_advisory_lock — outra execução "
                           "parece estar em andamento no mesmo Postgres")
        sessao_execucao.commit()
        sessao_execucao.close()
        return 1

    try:
        run_etapa = repo.run_etapa_por_id(sessao_execucao, run_etapa_id)
        if run_etapa is None:
            return 1
        run = repo.run_por_id(sessao_execucao, run_etapa["run_id"])
        definicao = registry.obter(run_etapa["etapa"])
        params = definicao.params_model(**run_etapa["params_efetivos"])

        repo.heartbeat(sessao_execucao, run_etapa_id, os.getpid())
        lock.renovar(sessao_execucao, run_etapa_id)
        sessao_execucao.commit()

        teto = run["teto_custo_usd"]
        db_etapa = db.create_session()
        ctx = DbContext(
            db_etapa, sessao_execucao=sessao_execucao, run_id=run_etapa["run_id"],
            run_etapa_id=run_etapa_id, etapa=run_etapa["etapa"],
            acao=run_etapa["acao"] or "atualizar", modo=run["modo"],
            teto_custo_usd=float(teto) if teto is not None else None,
        )

        codigo_saida = 0
        try:
            modulo = definicao.carregar()
            resultado = modulo.run(params, ctx)
            if ctx.cancelado():
                repo.marcar_cancelada(sessao_execucao, run_etapa_id)
                sessao_execucao.commit()
            else:
                fp = fingerprint.calcular_para_etapa(
                    sessao_execucao, run_etapa["etapa"], run_etapa["params_efetivos"])
                repo.marcar_concluida(
                    sessao_execucao, run_etapa_id, processados=resultado.processados,
                    erros=resultado.erros, metricas=resultado.metricas, fingerprint=fp)
                sessao_execucao.commit()
                _notificar_best_effort(
                    run_etapa["run_id"], run_etapa["etapa"], "concluida",
                    f"{resultado.processados} processados, {resultado.erros} erros")
        except TetoDeCustoExcedido as exc:
            repo.marcar_falhou(sessao_execucao, run_etapa_id, str(exc))
            sessao_execucao.commit()
            codigo_saida = 2
            _notificar_best_effort(run_etapa["run_id"], run_etapa["etapa"], "falhou", str(exc))
        except Exception:
            erro = traceback.format_exc()[-4000:]
            repo.marcar_falhou(sessao_execucao, run_etapa_id, erro)
            sessao_execucao.commit()
            codigo_saida = 1
            _notificar_best_effort(run_etapa["run_id"], run_etapa["etapa"], "falhou", erro)
        finally:
            db_etapa.close()
        return codigo_saida
    finally:
        # Libera SEMPRE, mesmo se `run_etapa`/`run` não existiam — senão o lock fica preso
        # até a lease expirar por um erro que nem chegou a executar nada.
        lock.liberar(sessao_execucao, run_etapa_id)
        lock.advisory_liberar(sessao_execucao)
        sessao_execucao.commit()
        sessao_execucao.close()


def main() -> None:
    if len(sys.argv) != 2:
        print("uso: python -m pesquisa_precos.runner.worker <run_etapa_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(executar(int(sys.argv[1])))


if __name__ == "__main__":
    main()
