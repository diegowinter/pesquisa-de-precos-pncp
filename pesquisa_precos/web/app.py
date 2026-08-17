"""
Interface web — Jinja2 + HTMX + Alpine.js, sem bundler (ADR-003, docs/04_FASES.md Fase 5).
"Um processo, duas superfícies" (docs/06_API_E_WEB.md §1): esta app importa só `services/` e
`db/` — nunca `etapas/`/`runner/` direto — e usa exatamente os mesmos serviços que `api/`.

Suba com `python -m pesquisa_precos.web` ou `uvicorn pesquisa_precos.web.app:app --reload`.
Porta sugerida 8001, para conviver com a API (Fase 4) na 8000 durante desenvolvimento.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pesquisa_precos.services import execucao as servico
from pesquisa_precos.services.execucao import (
    DependenciaNaoSatisfeita,
    ExecucaoEmAndamento,
    RefazerSemConfirmacao,
    RunInexistente,
)
from pesquisa_precos.web import auth
from pesquisa_precos.web.estado import CLASSE_ETAPA, ICONE_ETAPA

RAIZ_WEB = Path(__file__).resolve().parent

app = FastAPI(title="Pesquisa de Preços PLASEG — Web")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("WEB_SECRET_KEY") or os.urandom(32).hex())
app.mount("/static", StaticFiles(directory=RAIZ_WEB / "static"), name="static")

templates = Jinja2Templates(directory=RAIZ_WEB / "templates")
templates.env.globals["icone_etapa"] = ICONE_ETAPA
templates.env.globals["classe_etapa"] = CLASSE_ETAPA


def _render(request: Request, nome: str, contexto: dict[str, Any] | None = None, status_code: int = 200):
    ctx = {"usuario": request.session.get("usuario"), "erro": request.query_params.get("erro")}
    ctx.update(contexto or {})
    return templates.TemplateResponse(request, nome, ctx, status_code=status_code)


def _redirecionar_com_erro(url: str, exc: Exception) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"{url}?erro={quote(str(exc))}", status_code=303)


# ── Autenticação ──────────────────────────────────────────────────────────────────────

@app.get("/")
def raiz():
    return RedirectResponse("/runs", status_code=303)


@app.get("/login")
def tela_login(request: Request):
    if auth.autenticado(request):
        return RedirectResponse("/runs", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"usuario": None, "erro": request.query_params.get("erro"),
         "senha_exigida": auth.senha_exigida()})


@app.post("/login")
def fazer_login(request: Request, usuario: str = Form(...), senha: str = Form("")):
    if not auth.tentar_login(request, senha, usuario):
        return RedirectResponse("/login?erro=senha%20inv%C3%A1lida", status_code=303)
    return RedirectResponse("/runs", status_code=303)


@app.post("/logout")
def fazer_logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)


# ── Runs ──────────────────────────────────────────────────────────────────────────────

@app.get("/runs")
def lista_runs(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "runs_lista.html", {"runs": servico.listar_runs()})


@app.post("/runs")
def criar_run(request: Request, rotulo: str = Form(...), modo: str = Form("assistido"),
             teto_custo_usd: str = Form(""), usuario: str = Depends(auth.exigir_login)):
    run_id = servico.criar_run(rotulo, modo=modo,
                               teto_custo_usd=float(teto_custo_usd) if teto_custo_usd else None,
                               criado_por=usuario)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def hub_run(request: Request, run_id: int, usuario: str = Depends(auth.exigir_login)):
    run = servico.obter_run(run_id)
    if run is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "run_hub.html", {"run": run})


@app.get("/runs/{run_id}/grafo")
def fragmento_grafo(request: Request, run_id: int, usuario: str = Depends(auth.exigir_login)):
    run = servico.obter_run(run_id)
    if run is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "_grafo.html", {"run": run})


@app.post("/runs/{run_id}/abortar")
def abortar_run(request: Request, run_id: int, usuario: str = Depends(auth.exigir_login)):
    try:
        servico.abortar_run(run_id)
    except RunInexistente as exc:
        return _redirecionar_com_erro("/runs", exc)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── Etapa ─────────────────────────────────────────────────────────────────────────────

def _contexto_etapa(run_id: int, chave: str) -> dict[str, Any] | None:
    from pesquisa_precos.etapas import registry

    run = servico.obter_run(run_id)
    if run is None:
        return None
    detalhe = servico.detalhe_etapa(run_id, chave)
    estimativa, estimativa_erro = None, None
    try:
        estimativa = servico.estimativa_etapa(chave)
    except Exception as exc:  # noqa: BLE001 — estimativa é auxiliar; a tela não pode cair por causa dela
        estimativa_erro = str(exc)
    return {
        "run": run, "chave": chave, "definicao": registry.obter(chave), "detalhe": detalhe,
        "estimativa": estimativa, "estimativa_erro": estimativa_erro,
        "erros": servico.erros(run_id, etapa=chave),
        "logs": list(reversed(servico.logs(run_id, etapa=chave, limite=200))),
    }


@app.get("/runs/{run_id}/etapas/{chave}")
def tela_etapa(request: Request, run_id: int, chave: str, usuario: str = Depends(auth.exigir_login)):
    try:
        ctx = _contexto_etapa(run_id, chave)
    except KeyError as exc:
        return _redirecionar_com_erro(f"/runs/{run_id}", exc)
    if ctx is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "etapa.html", ctx)


@app.get("/runs/{run_id}/etapas/{chave}/fragmento")
def fragmento_etapa(request: Request, run_id: int, chave: str, usuario: str = Depends(auth.exigir_login)):
    ctx = _contexto_etapa(run_id, chave)
    if ctx is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "_etapa_progresso.html", ctx)


@app.get("/runs/{run_id}/etapas/{chave}/log/stream")
def log_stream_etapa(run_id: int, chave: str, request: Request, usuario: str = Depends(auth.exigir_login)):
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    async def eventos():
        ultimo_id = 0
        while True:
            recentes = sorted(servico.logs(run_id, etapa=chave, limite=50), key=lambda l: l["id"])
            for linha in recentes:
                if linha["id"] > ultimo_id:
                    ultimo_id = linha["id"]
                    yield f"data: {json.dumps(linha, default=str, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(eventos(), media_type="text/event-stream")


@app.post("/runs/{run_id}/etapas/{chave}/executar")
def executar_etapa(request: Request, run_id: int, chave: str, acao: str = Form("atualizar"),
                   confirmar: bool = Form(False), usuario: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/etapas/{chave}"
    try:
        servico.executar_etapa(run_id, chave, acao=acao, confirmar=confirmar)
    except (RunInexistente, DependenciaNaoSatisfeita, ExecucaoEmAndamento,
            RefazerSemConfirmacao) as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/etapas/{chave}/cancelar")
def cancelar_etapa(request: Request, run_id: int, chave: str, usuario: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/etapas/{chave}"
    try:
        servico.cancelar_etapa(run_id, chave)
    except RunInexistente as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/etapas/{chave}/aprovar")
def aprovar_etapa(request: Request, run_id: int, chave: str, params_override: str = Form("{}"),
                  usuario: str = Depends(auth.exigir_login)):
    import json

    voltar = f"/runs/{run_id}/etapas/{chave}"
    try:
        override = json.loads(params_override) if params_override.strip() else {}
    except json.JSONDecodeError as exc:
        return _redirecionar_com_erro(voltar, ValueError(f"params_override não é JSON válido: {exc}"))
    try:
        servico.aprovar_etapa(run_id, chave, aprovado_por=usuario, params_override=override)
        servico.executar_etapa(run_id, chave, acao="atualizar")
    except (RunInexistente, DependenciaNaoSatisfeita, ExecucaoEmAndamento) as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/etapas/{chave}/pular")
def pular_etapa(request: Request, run_id: int, chave: str, motivo: str = Form(""),
                usuario: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/etapas/{chave}"
    try:
        servico.pular_etapa(run_id, chave, motivo=motivo or f"pulada por {usuario}")
    except RunInexistente as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── Custo e exports ───────────────────────────────────────────────────────────────────

@app.get("/custo")
def dashboard_custo(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "custo.html", {"resumo": servico.custo_resumo()})


@app.get("/exports")
def tela_exports(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "exports.html", {"exports": servico.listar_exports()})


@app.get("/exports/{export_id}/download")
def baixar_export(export_id: int, usuario: str = Depends(auth.exigir_login)):
    export, caminho = servico.caminho_export(export_id)
    if export is None or not caminho.exists():
        return RedirectResponse("/exports?erro=export%20n%C3%A3o%20encontrado", status_code=303)
    return FileResponse(caminho, filename=caminho.name)
