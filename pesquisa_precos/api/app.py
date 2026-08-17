"""
FastAPI — comandar e observar a pipeline por HTTP (docs/04_FASES.md Fase 4,
docs/06_API_E_WEB.md). "Um processo, duas superfícies": `api/` devolve JSON, `web/` (Fase 5)
devolverá HTML, ambos consumindo os mesmos `services/`.

Invariante herdada de ADR-002: este processo NUNCA executa uma etapa na própria thread.
`POST .../executar` só grava intenção e sobe um subprocesso pelo `runner` — a rota volta na
hora, nunca bloqueia até a etapa terminar.

Suba com `python -m pesquisa_precos.api` ou `uvicorn pesquisa_precos.api.app:app --reload`.
"""

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from pesquisa_precos.api.auth import exigir_token
from pesquisa_precos.api.routers import config, notificacoes, runs
from pesquisa_precos.db import sessao as db
from pesquisa_precos.services import execucao as servico
from pesquisa_precos.services.execucao import (
    DependenciaNaoSatisfeita,
    ExecucaoEmAndamento,
    RefazerSemConfirmacao,
    RunInexistente,
)

app = FastAPI(title="Pesquisa de Preços PLASEG — API", version="0.1.0")
app.include_router(runs.router, prefix="/api", dependencies=[Depends(exigir_token)])
app.include_router(config.router, prefix="/api", dependencies=[Depends(exigir_token)])
app.include_router(notificacoes.router, prefix="/api", dependencies=[Depends(exigir_token)])


def _erro(codigo: str, mensagem: str, status_code: int) -> JSONResponse:
    """Formato de erro estável de docs/06_API_E_WEB.md §3.3: `{erro: {codigo, mensagem}}`,
    com `codigo` legível para o cliente decidir o que fazer sem parsear texto livre."""
    return JSONResponse(status_code=status_code,
                        content={"erro": {"codigo": codigo, "mensagem": mensagem}})


@app.exception_handler(ExecucaoEmAndamento)
async def _tratar_execucao_em_andamento(request: Request, exc: ExecucaoEmAndamento):
    return _erro("EXECUCAO_EM_ANDAMENTO", str(exc), 409)


@app.exception_handler(DependenciaNaoSatisfeita)
async def _tratar_dependencia(request: Request, exc: DependenciaNaoSatisfeita):
    return _erro("DEPENDENCIA_NAO_SATISFEITA", str(exc), 422)


@app.exception_handler(RefazerSemConfirmacao)
async def _tratar_confirmacao(request: Request, exc: RefazerSemConfirmacao):
    return _erro("CONFIRMACAO_NECESSARIA", str(exc), 422)


@app.exception_handler(RunInexistente)
async def _tratar_run_inexistente(request: Request, exc: RunInexistente):
    return _erro("NAO_ENCONTRADO", str(exc), 404)


@app.exception_handler(KeyError)
async def _tratar_chave_invalida(request: Request, exc: KeyError):
    return _erro("NAO_ENCONTRADO", str(exc).strip("'\""), 404)


@app.get("/api/health")
def health():
    """Sem autenticação — é o endpoint que um monitor externo bate antes de saber se vale a
    pena mandar um token."""
    ok, mensagem = db.esta_disponivel()
    return {"status": "ok" if ok else "erro", "banco": mensagem, "versao": app.version}


@app.get("/api/providers/status")
def providers_status(_: None = Depends(exigir_token)):
    return {"provedores": servico.listar_provedores()}
