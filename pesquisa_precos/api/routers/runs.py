"""
Rotas `/api/runs*` — leitura e comando sobre runs/etapas (docs/06_API_E_WEB.md §3, entrega da
Fase 4 em docs/04_FASES.md). Toda rota chama `services/execucao`, nunca `db/repos` ou
`runner/*` direto (docs/01_ARQUITETURA.md §7).

SSE (`/log/stream`, `/progresso/stream`): sem fila/pubsub — ADR-001 é explícito que este é um
sistema single-tenant/single-writer sem infra extra, e a cardinalidade de leitores é a mesma
pessoa com uma aba aberta. O stream é só um polling curto sobre o banco.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from pesquisa_precos.api.schemas import AprovarEtapaBody, CriarRunBody, ExecutarEtapaBody
from pesquisa_precos.services import diff as servico_diff
from pesquisa_precos.services import execucao as servico
from pesquisa_precos.services.diff import RunSemRankingError

router = APIRouter(tags=["runs"])


@router.get("/runs")
def listar_runs(limite: int = Query(50, le=200)):
    return servico.listar_runs(limite)


@router.get("/runs/diff")
def diff_runs(run_a: int, run_b: int, limiar_variacao: float = 0.0):
    """Fase 9, item 2: "o que mudou do export de ontem para o de hoje" — generaliza o
    `--novos` da etapa 8 para comparar dois runs quaisquer (não só run × snapshot)."""
    try:
        return servico_diff.diff_runs(run_a, run_b, limiar_variacao=limiar_variacao)
    except RunSemRankingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/runs", status_code=201)
def criar_run(body: CriarRunBody):
    run_id = servico.criar_run(
        body.rotulo, modo=body.modo, config_rotulo=body.config_rotulo,
        teto_custo_usd=body.teto_custo_usd, criado_por=body.criado_por)
    return servico.obter_run(run_id)


@router.get("/runs/{run_id}")
def obter_run(run_id: int):
    run = servico.obter_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} não existe")
    return run


@router.get("/runs/{run_id}/etapas/{chave}")
def detalhe_etapa(run_id: int, chave: str):
    return servico.detalhe_etapa(run_id, chave)


@router.get("/runs/{run_id}/etapas/{chave}/estimativa")
def estimativa_etapa(run_id: int, chave: str):
    if servico.obter_run(run_id) is None:
        raise HTTPException(404, f"run {run_id} não existe")
    return servico.estimativa_etapa(chave)


@router.post("/runs/{run_id}/etapas/{chave}/executar", status_code=202)
def executar_etapa(run_id: int, chave: str, body: ExecutarEtapaBody):
    return servico.executar_etapa(
        run_id, chave, acao=body.acao, params_override=body.params_override,
        confirmar=body.confirmar)


@router.post("/runs/{run_id}/etapas/{chave}/cancelar")
def cancelar_etapa(run_id: int, chave: str):
    return {"cancelada": servico.cancelar_etapa(run_id, chave)}


@router.post("/runs/{run_id}/etapas/{chave}/aprovar")
def aprovar_etapa(run_id: int, chave: str, body: AprovarEtapaBody):
    return servico.aprovar_etapa(
        run_id, chave, aprovado_por=body.aprovado_por, params_override=body.params_override)


@router.get("/runs/{run_id}/log")
def log_run(run_id: int, etapa: str | None = None, n: int = Query(200, le=1000)):
    return servico.logs(run_id, etapa=etapa, limite=n)


@router.get("/runs/{run_id}/erros")
def erros_run(run_id: int, etapa: str | None = None):
    return servico.erros(run_id, etapa=etapa)


@router.get("/runs/{run_id}/custo")
def custo_run(run_id: int):
    return servico.custo(run_id)


async def _eventos_log(run_id: int, etapa: str | None) -> AsyncIterator[str]:
    ultimo_id = 0
    while True:
        recentes = sorted(servico.logs(run_id, etapa=etapa, limite=50), key=lambda l: l["id"])
        for linha in recentes:
            if linha["id"] > ultimo_id:
                ultimo_id = linha["id"]
                yield f"data: {json.dumps(linha, default=str, ensure_ascii=False)}\n\n"
        await asyncio.sleep(1.5)


@router.get("/runs/{run_id}/log/stream")
def log_stream(run_id: int, etapa: str | None = None):
    return StreamingResponse(_eventos_log(run_id, etapa), media_type="text/event-stream")


async def _eventos_progresso(run_id: int) -> AsyncIterator[str]:
    anterior: str | None = None
    while True:
        run = servico.obter_run(run_id)
        if run is None:
            yield 'event: erro\ndata: {"mensagem": "run não existe"}\n\n'
            return
        atual = json.dumps(run["etapas"], default=str, ensure_ascii=False)
        if atual != anterior:
            anterior = atual
            yield f"data: {atual}\n\n"
        await asyncio.sleep(2)


@router.get("/runs/{run_id}/progresso/stream")
def progresso_stream(run_id: int):
    return StreamingResponse(_eventos_progresso(run_id), media_type="text/event-stream")
