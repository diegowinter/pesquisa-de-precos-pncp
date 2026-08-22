"""
Camada de serviço entre `api/` (e, na Fase 5, `web/`) e `db/`/`runner/` (docs/01_ARQUITETURA.md
§7: "api/ e web/ importam apenas services/ e db/ — nunca etapas/ diretamente"; §1: "web/ e api/
consomem os mesmos serviços"). Nada de SQL solto aqui — o que falta em `db/repos/execucao.py`
foi acrescentado lá, não reescrito à mão neste módulo.

Gate (docs/06_API_E_WEB.md §4.3, ADR-005): aprovação é um `UPDATE`, não um processo à espera.
Uma etapa com `precisa_gate=True` e ainda sem `aprovado_em` neste `run_etapa` fica
`aguardando_aprovacao` em vez de subir subprocesso; `aprovar_etapa()` grava a aprovação (e,
se houver, o `params_override` editado) e volta o status a `nao_iniciada` — só a PRÓXIMA
chamada a `executar_etapa()` é que efetivamente sobe o subprocesso (ADR-005: "gate não segura
o lock — o processo já terminou").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo
from pesquisa_precos.etapas import registry
from pesquisa_precos.runner import executor, lock
from pesquisa_precos.services import notificacoes


class RunInexistente(RuntimeError):
    """`run_id` não existe — 404 na API."""


class ExecucaoEmAndamento(RuntimeError):
    """Lock ocupado por outra etapa (ADR-001: single-writer) — 409 na API."""


class DependenciaNaoSatisfeita(RuntimeError):
    """Etapa da qual esta depende ainda não concluiu neste run — 422 na API."""


class RefazerSemConfirmacao(RuntimeError):
    """`acao=refazer` sem `confirmar=true` — 422 na API (ADR-008: só `refazer` queima
    investimento, por isso é o único que exige confirmação explícita)."""


def listar_runs(limite: int = 50) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        linhas = sessao.execute(
            text("SELECT id, rotulo, modo, status, config_versao_id, teto_custo_usd, "
                 "       custo_usd, criado_em, concluido_em "
                 "FROM run ORDER BY id DESC LIMIT :n"), {"n": limite}).mappings().all()
        return [dict(linha) for linha in linhas]


def criar_run(rotulo: str, *, modo: str = "assistido", config_rotulo: str = "default",
              teto_custo_usd: float | None = None, criado_por: str | None = None) -> int:
    with db.sessao() as sessao:
        cv = repo.config_versao_por_rotulo(sessao, config_rotulo)
        if cv is None:
            cv = repo.criar_config_versao(sessao, config_rotulo, criado_por=criado_por,
                                          notas="criada pela API")
        run_id = repo.criar_run(sessao, rotulo, cv, modo=modo, criado_por=criado_por)
        if teto_custo_usd is not None:
            sessao.execute(text("UPDATE run SET teto_custo_usd = :t WHERE id = :id"),
                           {"t": teto_custo_usd, "id": run_id})
    return run_id


def obter_run(run_id: int) -> dict[str, Any] | None:
    """Run + uma linha por etapa do registry (docs/06_API_E_WEB.md §4.1, o "grafo"). Etapas
    ainda não tocadas neste run aparecem como `nao_iniciada` sem precisar de uma linha em
    `run_etapa` — só `obter_ou_criar_run_etapa` cria a linha de verdade, e isso só deve
    acontecer quando algo de fato age sobre a etapa (executar/aprovar/cancelar)."""
    with db.sessao() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            return None
        existentes = {linha["etapa"]: dict(linha) for linha in sessao.execute(
            text("SELECT etapa, status, processados, erros, custo_usd, heartbeat_em, "
                 "       concluida_em FROM run_etapa WHERE run_id = :r"),
            {"r": run_id}).mappings().all()}
    etapas = []
    for definicao in registry.ordem():
        base = existentes.get(definicao.chave, {
            "etapa": definicao.chave, "status": "nao_iniciada", "processados": 0, "erros": 0,
            "custo_usd": 0, "heartbeat_em": None, "concluida_em": None})
        etapas.append({**base, "titulo": definicao.titulo,
                       "depende_de": list(definicao.depende_de)})
    run["etapas"] = etapas
    return run


def detalhe_etapa(run_id: int, chave: str) -> dict[str, Any]:
    registry.obter(chave)  # KeyError -> 404 (tratado no app)
    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        return repo.run_etapa_por_id(sessao, run_etapa_id)


def estimativa_etapa(chave: str) -> dict[str, Any]:
    """`estimar()` da etapa — escopo e custo previstos, sem gastar nada. Roda fora de um run,
    então o contexto é o `ContextoNulo` (ver o módulo)."""
    from pesquisa_precos.config.settings import carregar_config
    from pesquisa_precos.runner.contexto_nulo import ContextoNulo

    definicao = registry.obter(chave)
    modulo = definicao.carregar()
    params = definicao.params_model()
    with ContextoNulo(chave, config=carregar_config()) as ctx:
        estimativa = modulo.estimar(params, ctx)
    return estimativa.model_dump()


def executar_etapa(run_id: int, chave: str, *, acao: str = "atualizar",
                   params_override: dict[str, Any] | None = None,
                   confirmar: bool = False) -> dict[str, Any]:
    definicao = registry.obter(chave)
    if acao == "refazer" and not confirmar:
        raise RefazerSemConfirmacao(
            f"'refazer' na etapa {chave} exige confirmar=true no corpo (ADR-008)")

    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        faltando = [dep for dep in definicao.depende_de if repo.status_run_etapa(
            sessao, repo.obter_ou_criar_run_etapa(sessao, run_id, dep)) != "concluida"]
        if faltando:
            raise DependenciaNaoSatisfeita(
                f"etapa {chave} depende de {', '.join(faltando)}, ainda não "
                f"concluída(s) neste run")

        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        linha = repo.run_etapa_por_id(sessao, run_etapa_id)
        if definicao.precisa_gate and linha["aprovado_em"] is None:
            repo.gravar_params(sessao, run_etapa_id,
                               params_efetivos=linha["params_efetivos"] or {},
                               params_override=params_override or {})
            repo.marcar_aguardando_aprovacao(sessao, run_etapa_id)
            try:
                notificacoes.notificar_evento(run_id, chave, "aguardando_aprovacao")
            except Exception:  # noqa: BLE001 — best-effort (docs/04_FASES.md Fase 9 item 3)
                pass
            return {"run_etapa_id": run_etapa_id, "status": "aguardando_aprovacao", "pid": None}

    try:
        run_etapa_id, processo = executor.tocar(
            run_id, chave, acao=acao, params_override=params_override or {})
    except lock.LockOcupado as exc:
        raise ExecucaoEmAndamento(str(exc)) from exc
    return {"run_etapa_id": run_etapa_id, "status": "executando", "pid": processo.pid}


def cancelar_etapa(run_id: int, chave: str) -> bool:
    registry.obter(chave)
    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        return repo.solicitar_cancelamento(sessao, run_etapa_id)


def aprovar_etapa(run_id: int, chave: str, *, aprovado_por: str,
                  params_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Toda edição do gate vira `params_override` (docs/06_API_E_WEB.md §4.3) — reaproveita
    `executor.preparar()` para resolver as três camadas corretamente em vez de reimplementar a
    resolução aqui."""
    registry.obter(chave)
    if params_override:
        executor.preparar(run_id, chave, params_override=params_override)
    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        repo.aprovar(sessao, run_etapa_id, aprovado_por)
    return {"run_etapa_id": run_etapa_id, "status": "nao_iniciada"}


def pular_etapa(run_id: int, chave: str, *, motivo: str | None = None) -> bool:
    registry.obter(chave)
    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        return repo.pular(sessao, run_etapa_id, motivo)


def abortar_run(run_id: int) -> bool:
    with db.sessao() as sessao:
        if repo.run_por_id(sessao, run_id) is None:
            raise RunInexistente(f"run {run_id} não existe")
        return repo.abortar_run(sessao, run_id)


def logs(run_id: int, *, etapa: str | None = None, limite: int = 200) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.logs_do_run(sessao, run_id, etapa=etapa, limite=limite)


def erros(run_id: int, *, etapa: str | None = None) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.erros_do_run(sessao, run_id, etapa=etapa)


def custo(run_id: int) -> dict[str, Any]:
    with db.sessao() as sessao:
        run = repo.run_por_id(sessao, run_id)
        if run is None:
            raise RunInexistente(f"run {run_id} não existe")
        por_etapa = [dict(linha) for linha in sessao.execute(
            text("SELECT etapa, custo_usd FROM run_etapa WHERE run_id = :r AND custo_usd > 0 "
                 "ORDER BY etapa"), {"r": run_id}).mappings().all()]
    return {"run_id": run_id, "custo_usd": run["custo_usd"], "teto_custo_usd": run["teto_custo_usd"],
            "por_etapa": por_etapa}


def listar_provedores() -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.listar_provedores(sessao)


def custo_resumo(*, de: str | None = None, ate: str | None = None) -> dict[str, Any]:
    with db.sessao() as sessao:
        return repo.custo_resumo(sessao, de=de, ate=ate)


def listar_exports(*, run_id: int | None = None) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.listar_exports(sessao, run_id=run_id)


def caminho_export(export_id: int):
    """Caminho absoluto do arquivo de export, resolvido contra `paths.DATA` (`export.arquivo`
    é relativo — docs/08_CONVENCOES.md: nunca hardcodar caminho de `data/`). `None` se o
    registro não existe."""
    from pesquisa_precos.config import paths

    with db.sessao() as sessao:
        export = repo.export_por_id(sessao, export_id)
    if export is None:
        return None, None
    return export, paths.DATA / export["arquivo"]
