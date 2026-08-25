"""
Ponto de entrada do subprocesso de uma etapa (ADR-002) — o que `runner.launcher` sobe.

Uso: `python -m pesquisa_precos.runner.worker <run_etapa_id>`

O `run_step` já existe e já está `executando` quando este processo começa (é
`launcher.iniciar_subprocesso` quem faz isso, ANTES de subir o subprocesso — assim, se o
`Popen` falhar por qualquer razão do SO, o estado no banco continua consistente e não fica um
`run_step` fantasma sem processo nenhum tentando geri-lo).

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


def _notificar_best_effort(run_id: int, step: str, evento: str, detalhe: str = "") -> None:
    """Fase 9, item 3: dispara logo após o `commit` de estado (nunca antes — a notificação não
    pode fazer o `run_step` parecer concluído/falho antes de o banco de fato refletir isso).
    Envolvida numa exceção genérica: `notifications.notificar_evento` já é best-effort por
    dentro, mas esta é a segunda rede — nenhuma falha aqui pode propagar para o `except`
    principal e mascarar o motivo real de uma etapa ter falhado."""
    try:
        notifications.notificar_evento(run_id, step, evento, detalhe=detalhe)
    except Exception:  # noqa: BLE001 — ver docstring
        pass


def run(run_etapa_id: int) -> int:
    sessao_execucao = db.create_session()

    if not lock.advisory_tentar(sessao_execucao):
        # Não conseguir o advisory lock significa que OUTRO processo ainda está executando
        # esta etapa — ele é o dono legítimo da linha. Este processo apenas desiste: marcar
        # `failed` aqui carimbaria de fracassada uma execução VIVA, que foi o que aconteceu em
        # 2026-08-24 (o worker antigo seguia coletando e a tela passou a mostrar "falhou").
        linha = repo.run_etapa_por_id(sessao_execucao, run_etapa_id)
        repo.registrar_log(
            sessao_execucao, linha["run_id"] if linha else 0,
            linha["step"] if linha else None, "aviso",
            "[yellow]Já existe um processo executando esta step (advisory lock ocupado) — "
            "esta tentativa foi descartada e a execução em andamento segue.[/]")
        sessao_execucao.commit()
        sessao_execucao.close()
        return 1

    try:
        # O PREÂMBULO também marca `failed` (2026-08-23). Antes, qualquer erro aqui — params
        # inválidos, step fora do registry, import da etapa — subia direto para o `finally`,
        # que liberava o lock e encerrava o processo: a linha ficava `running` para sempre, sem
        # log, sem heartbeat e sem mensagem, e a tela mostrava uma etapa "em andamento" que já
        # tinha morrido. Falha silenciosa é pior que falha barulhenta.
        try:
            run_step = repo.run_etapa_por_id(sessao_execucao, run_etapa_id)
            if run_step is None:
                return 1
            run = repo.run_por_id(sessao_execucao, run_step["run_id"])
            definicao = registry.obter(run_step["step"])
            params = definicao.params_model(**run_step["effective_params"])

            repo.heartbeat(sessao_execucao, run_etapa_id, os.getpid())
            lock.renovar(sessao_execucao, run_etapa_id)
            sessao_execucao.commit()

            teto = run["cost_cap_usd"]
            db_etapa = db.create_session()
            ctx = DbContext(
                db_etapa, sessao_execucao=sessao_execucao, run_id=run_step["run_id"],
                run_etapa_id=run_etapa_id, step=run_step["step"],
                action=run_step["action"] or "update", mode=run["mode"],
                cost_cap_usd=float(teto) if teto is not None else None,
            )
        except Exception:
            erro = traceback.format_exc()[-4000:]
            print(erro, file=sys.stderr)
            sessao_execucao.rollback()
            repo.marcar_falhou(sessao_execucao, run_etapa_id,
                               "falha ao preparar a execução:\n" + erro)
            sessao_execucao.commit()
            return 1

        codigo_saida = 0
        ctx.iniciar_heartbeat()
        erros_antes = repo.erros_pendentes_tentativas(
            sessao_execucao, run_step["run_id"], run_step["step"])
        try:
            modulo = definicao.carregar()
            resultado = modulo.run(params, ctx)
            if ctx.cancelado():
                repo.marcar_cancelada(sessao_execucao, run_etapa_id)
                sessao_execucao.commit()
            else:
                fp = fingerprint.calcular_para_etapa(
                    sessao_execucao, run_step["step"], run_step["effective_params"])
                # `resumo` viaja dentro de metrics: é texto de exibição, não merece coluna
                # (e uma migration no meio de um teste assistido é fricção sem retorno).
                metricas = dict(resultado.metrics)
                if resultado.resumo:
                    metricas["resumo"] = resultado.resumo
                # O que estava pendente e não falhou de novo está resolvido.
                depois = repo.erros_pendentes_tentativas(
                    sessao_execucao, run_step["run_id"], run_step["step"])
                repo.resolver_erros(
                    sessao_execucao, run_step["run_id"], run_step["step"],
                    [k for k, n in erros_antes.items() if depois.get(k, n) == n])
                repo.marcar_concluida(
                    sessao_execucao, run_etapa_id, processed=resultado.processed,
                    errors=resultado.errors, metrics=metricas, fingerprint=fp)
                sessao_execucao.commit()
                _notificar_best_effort(
                    run_step["run_id"], run_step["step"], "finished",
                    f"{resultado.processed} processed, {resultado.errors} erros")
        except TetoDeCustoExcedido as exc:
            repo.marcar_falhou(sessao_execucao, run_etapa_id, str(exc))
            sessao_execucao.commit()
            codigo_saida = 2
            _notificar_best_effort(run_step["run_id"], run_step["step"], "failed", str(exc))
        except SystemExit as exc:
            # As etapas usam `raise SystemExit("mensagem")` para pré-condição não satisfeita
            # ("nenhum termo ativo", "banco indisponível"). SystemExit NÃO é Exception: sem
            # este ramo ela subia até o `finally`, o processo morria calado e a linha ficava
            # `running` para sempre — a etapa 2 passou uma hora assim em 2026-08-23.
            mensagem = str(exc) or "a step encerrou sem mensagem"
            repo.marcar_falhou(sessao_execucao, run_etapa_id, mensagem)
            sessao_execucao.commit()
            codigo_saida = 1
            _notificar_best_effort(run_step["run_id"], run_step["step"], "failed", mensagem)
        except Exception:
            erro = traceback.format_exc()[-4000:]
            repo.marcar_falhou(sessao_execucao, run_etapa_id, erro)
            sessao_execucao.commit()
            codigo_saida = 1
            _notificar_best_effort(run_step["run_id"], run_step["step"], "failed", erro)
        finally:
            ctx.encerrar()
            db_etapa.close()
        return codigo_saida
    finally:
        # Libera SEMPRE, mesmo se `run_step`/`run` não existiam — senão o lock fica preso
        # até a lease expirar por um erro que nem chegou a executar nada.
        lock.liberar(sessao_execucao, run_etapa_id)
        lock.advisory_liberar(sessao_execucao)
        sessao_execucao.commit()
        sessao_execucao.close()


def main() -> None:
    if len(sys.argv) != 2:
        print("uso: python -m pesquisa_precos.runner.worker <run_etapa_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(int(sys.argv[1])))


if __name__ == "__main__":
    main()
