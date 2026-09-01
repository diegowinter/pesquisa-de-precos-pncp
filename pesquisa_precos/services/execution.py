"""
Camada de serviço entre `api/` (e, na Fase 5, `web/`) e `db/`/`runner/` (docs/01_ARQUITETURA.md
§7: "api/ e web/ importam apenas services/ e db/ — nunca etapas/ diretamente"; §1: "web/ e api/
consomem os mesmos serviços"). Nada de SQL solto aqui — o que falta em `db/repos/execucao.py`
foi acrescentado lá, não reescrito à mão neste módulo.

Gate (docs/06_API_E_WEB.md §4.3, ADR-005): aprovação é um `UPDATE`, não um processo à espera.
Uma etapa com `precisa_gate=True` e ainda sem `approved_at` neste `run_step` fica
`aguardando_aprovacao` em vez de subir subprocesso; `aprovar_etapa()` grava a aprovação (e,
se houver, o `params_override` editado) e volta o status a `nao_iniciada` — só a PRÓXIMA
chamada a `executar_etapa()` é que efetivamente sobe o subprocesso (ADR-005: "gate não segura
o lock — o processo já terminou").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps import registry
from pesquisa_precos.runner import launcher, lock
from pesquisa_precos.services import notifications


class RunInexistente(RuntimeError):
    """`run_id` não existe — 404 na API."""


class ExecucaoEmAndamento(RuntimeError):
    """Lock ocupado por outra etapa (ADR-001: single-writer) — 409 na API."""


class DependenciaNaoSatisfeita(RuntimeError):
    """Etapa da qual esta depende ainda não concluiu neste run — 422 na API."""


class RefazerSemConfirmacao(RuntimeError):
    """`action=refazer` sem `confirm=true` — 422 na API (ADR-008: só `refazer` queima
    investimento, por isso é o único que exige confirmação explícita)."""


def listar_runs(limite: int = 50) -> list[dict[str, Any]]:
    with db.session() as sessao:
        linhas = sessao.execute(
            text("SELECT id, label, mode, status, config_version_id, cost_cap_usd, "
                 "       cost_usd, created_at, finished_at "
                 "FROM run ORDER BY id DESC LIMIT :n"), {"n": limite}).mappings().all()
        return [dict(linha) for linha in linhas]


def criar_run(label: str, *, mode: str = "assisted", config_rotulo: str = "default",
              cost_cap_usd: float | None = None, created_by: str | None = None) -> int:
    with db.session() as sessao:
        cv = repo.config_versao_por_rotulo(sessao, config_rotulo)
        if cv is None:
            cv = repo.criar_config_versao(sessao, config_rotulo, created_by=created_by,
                                          notes="criada pela API")
        run_id = repo.criar_run(sessao, label, cv, mode=mode, created_by=created_by)
        if cost_cap_usd is not None:
            sessao.execute(text("UPDATE run SET cost_cap_usd = :t WHERE id = :id"),
                           {"t": cost_cap_usd, "id": run_id})
    return run_id


def obter_run(run_id: int) -> dict[str, Any] | None:
    """Run + uma linha por etapa do registry (docs/06_API_E_WEB.md §4.1, o "grafo"). Etapas
    ainda não tocadas neste run aparecem como `nao_iniciada` sem precisar de uma linha em
    `run_step` — só `obter_ou_criar_run_etapa` cria a linha de verdade, e isso só deve
    acontecer quando algo de fato age sobre a etapa (executar/approve/cancel)."""
    with db.session() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            return None
        existentes = {linha["step"]: dict(linha) for linha in sessao.execute(
            text("SELECT step, status, processed, errors, cost_usd, heartbeat_at, "
                 "       finished_at FROM run_step WHERE run_id = :r"),
            {"r": run_id}).mappings().all()}
    etapas = []
    for definicao in registry.ordem():
        base = existentes.get(definicao.key, {
            "step": definicao.key, "status": "not_started", "processed": 0, "erros": 0,
            "cost_usd": 0, "heartbeat_at": None, "finished_at": None})
        etapas.append({**base, "titulo": definicao.titulo,
                       "depends_on": list(definicao.depends_on)})
    run["steps"] = etapas
    # Nada marca o run como `finished` no banco: ele só sai de `open` ao ser abortado. Então
    # "acabou" é derivado — toda etapa do registry concluída ou pulada. Sem isso a tela
    # oferecia "abortar run" numa run que já tinha entregado o export.
    run["completa"] = all(e["status"] in ("finished", "skipped") for e in etapas)
    return run


def detalhe_etapa(run_id: int, key: str) -> dict[str, Any]:
    registry.obter(key)  # KeyError -> 404 (tratado no app)
    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        return repo.run_etapa_por_id(sessao, run_etapa_id)


def estimativa_etapa(key: str) -> dict[str, Any]:
    """`estimar()` da etapa — escopo e custo previstos, sem gastar nada. Roda fora de um run,
    então o contexto é o `NullContext` (ver o módulo)."""
    from pesquisa_precos.runner.null_context import NullContext

    definicao = registry.obter(key)
    modulo = definicao.carregar()
    params = definicao.params_model()
    with NullContext(key) as ctx:
        estimativa = modulo.estimate(params, ctx)
    return estimativa.model_dump()


def executar_etapa(run_id: int, key: str, *, action: str = "update",
                   params_override: dict[str, Any] | None = None,
                   confirm: bool = False) -> dict[str, Any]:
    definicao = registry.obter(key)
    if action == "redo" and not confirm:
        raise RefazerSemConfirmacao(
            f"'redo' na step {key} exige confirm=true no corpo (ADR-008)")

    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        faltando = [dep for dep in definicao.depends_on if repo.status_run_etapa(
            sessao, repo.obter_ou_criar_run_etapa(sessao, run_id, dep)) != "finished"]
        if faltando:
            raise DependenciaNaoSatisfeita(
                f"step {key} depende de {', '.join(faltando)}, ainda não "
                f"concluída(s) neste run")

        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        linha = repo.run_etapa_por_id(sessao, run_etapa_id)
        if definicao.precisa_gate and linha["approved_at"] is None:
            repo.gravar_params(sessao, run_etapa_id,
                               effective_params=linha["effective_params"] or {},
                               params_override=params_override or {})
            repo.marcar_aguardando_aprovacao(sessao, run_etapa_id)
            try:
                notifications.notificar_evento(run_id, key, "awaiting_approval")
            except Exception:  # noqa: BLE001 — best-effort (docs/04_FASES.md Fase 9 item 3)
                pass
            return {"run_etapa_id": run_etapa_id, "status": "awaiting_approval", "pid": None}

    try:
        run_etapa_id, processo = launcher.tocar(
            run_id, key, action=action, params_override=params_override or {})
    except lock.LockOcupado as exc:
        raise ExecucaoEmAndamento(str(exc)) from exc
    return {"run_etapa_id": run_etapa_id, "status": "running", "pid": processo.pid}


def cancelar_etapa(run_id: int, key: str) -> bool:
    registry.obter(key)
    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        return repo.solicitar_cancelamento(sessao, run_etapa_id)


def aprovar_etapa(run_id: int, key: str, *, approved_by: str,
                  params_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Toda edição do gate vira `params_override` (docs/06_API_E_WEB.md §4.3) — reaproveita
    `launcher.preparar()` para resolver as três camadas corretamente em vez de reimplementar a
    resolução aqui."""
    registry.obter(key)
    if params_override:
        launcher.preparar(run_id, key, params_override=params_override)
    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        repo.aprovar(sessao, run_etapa_id, approved_by)
    return {"run_etapa_id": run_etapa_id, "status": "not_started"}


def pular_etapa(run_id: int, key: str, *, reason: str | None = None) -> bool:
    registry.obter(key)
    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, key)
        return repo.pular(sessao, run_etapa_id, reason)


def abortar_run(run_id: int) -> bool:
    with db.session() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        return repo.abortar_run(sessao, run_id)


def logs(run_id: int, *, step: str | None = None, limite: int = 200) -> list[dict[str, Any]]:
    with db.session() as sessao:
        return repo.logs_do_run(sessao, run_id, step=step, limite=limite)


def erros(run_id: int, *, step: str | None = None) -> list[dict[str, Any]]:
    with db.session() as sessao:
        return repo.erros_do_run(sessao, run_id, step=step)


def custo(run_id: int) -> dict[str, Any]:
    with db.session() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            raise RunInexistente(f"run {run_id} não existe")
        por_etapa = [dict(linha) for linha in sessao.execute(
            text("SELECT step, cost_usd FROM run_step WHERE run_id = :r AND cost_usd > 0 "
                 "ORDER BY step"), {"r": run_id}).mappings().all()]
    return {"run_id": run_id, "cost_usd": run["cost_usd"], "cost_cap_usd": run["cost_cap_usd"],
            "por_etapa": por_etapa}


def listar_provedores() -> list[dict[str, Any]]:
    with db.session() as sessao:
        return repo.listar_provedores(sessao)


def custo_resumo(*, de: str | None = None, ate: str | None = None) -> dict[str, Any]:
    with db.session() as sessao:
        return repo.custo_resumo(sessao, de=de, ate=ate)


def listar_exports(*, run_id: int | None = None) -> list[dict[str, Any]]:
    with db.session() as sessao:
        return repo.listar_exports(sessao, run_id=run_id)


def conteudo_export(export_id: int):
    """(registro, bytes do XLSX, nome do arquivo) — o export vive em `export.conteudo`
    (ADR-018 §2), não em disco. `(None, None, None)` se o registro não existe.

    Linhas geradas ANTES da Fase 10 têm `conteudo` nulo e só `arquivo` (um caminho relativo em
    `data/`). Elas voltam com `conteudo=None`: a interface avisa em vez de servir um arquivo
    que pode não estar mais lá — o `data/` local deixou de ser parte do sistema."""
    with db.session() as sessao:
        export = repo.export_por_id(sessao, export_id)
        if export is None:
            return None, None, None
        conteudo, name = repo.conteudo_export(sessao, export_id)
    return export, conteudo, name or f"export_{export_id}.xlsx"


def saude_provedores() -> list[dict[str, Any]]:
    """Sonda `chat`/`embed`/`rerank`/`extract`/`pareamento` e devolve o
    resultado. Não gasta e não dispara etapa — é a mesma sondagem HTTP leve que
    `runner.launcher` faz antes do play, exposta para diagnóstico manual.

    Era o comando `cli providers saude`; virou tela na Fase 13, quando a CLI saiu."""
    from pesquisa_precos.providers import health

    # `embed` NÃO é sondado como serviço próprio nem `ocr` aparece: ambos são consumidos
    # DENTRO dos serviços do companion, na máquina deles (ADR-021). Sondá-los daqui daria
    # linha vermelha permanente por um endereço que este processo nunca chama.
    capabilities = ["chat", "embed", "rerank", "extract", "matching"]

    def uma(capability: str, sessao) -> dict[str, Any]:
        # Uma capacidade que nem dá para resolver (schema do banco atrás do código, provedor
        # ausente) vira uma LINHA reprovada, não uma tela em branco: a tela de diagnóstico
        # precisa ser a última coisa a cair.
        try:
            return health.checar_capacidade(capability, sessao=sessao)
        except Exception as exc:  # noqa: BLE001 — ver acima
            if sessao is not None:
                sessao.rollback()
            return {"capability": capability, "provider": "—", "source": "—", "base_url": None,
                    "healthy": False, "latency_ms": None,
                    "message": f"não foi possível sondar: {type(exc).__name__}: {exc}"[:300]}

    if db.is_available()[0]:
        with db.session() as sessao:
            return [uma(c, sessao) for c in capabilities]
    return [uma(c, None) for c in capabilities]
