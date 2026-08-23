"""
Interface web — Jinja2 + HTMX + Alpine.js, sem bundler (ADR-003, docs/04_FASES.md Fase 5).
"Um processo, duas superfícies" (docs/06_API_E_WEB.md §1): esta app importa só `services/` e
`db/` — nunca `etapas/`/`runner/` direto.

Fase 13 (ADR-020): as duas superfícies passam a viver no MESMO processo e na MESMA porta. Os
routers JSON de `api/routers/` são montados aqui sob `/api`, protegidos por `X-API-Token`; o
HTML é protegido por sessão de cookie. Não existe mais `api/app.py` nem uma segunda porta.

Suba com `python -m pesquisa_precos` (ou `uvicorn pesquisa_precos.web.app:app --reload`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pesquisa_precos.api.auth import exigir_token
from pesquisa_precos.api.routers import config as router_config
from pesquisa_precos.api.routers import notifications as router_notifications
from pesquisa_precos.api.routers import runs as router_runs
from pesquisa_precos.db import session as db
from pesquisa_precos.db.secret import ChaveMestraAusente
from pesquisa_precos.services import config as service_config
from pesquisa_precos.services import diff as service_diff
from pesquisa_precos.services import execution as service
from pesquisa_precos.services import notification_recipients as service_recipients
from pesquisa_precos.services import prompts as service_prompts
from pesquisa_precos.services import providers as service_providers
from pesquisa_precos.services.config import ConfigVersaoInexistente
from pesquisa_precos.services.diff import RunSemRankingError
from pesquisa_precos.services.execution import (
    DependenciaNaoSatisfeita,
    ExecucaoEmAndamento,
    RefazerSemConfirmacao,
    RunInexistente,
)
from pesquisa_precos.services.notification_recipients import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)
from pesquisa_precos.services.prompts import PromptInexistente
from pesquisa_precos.services.providers import (
    FallbackProibido,
    ProvedorInexistente,
    InvalidProvider,
)
from pesquisa_precos.web import auth
from pesquisa_precos.web.state import CLASSE_STEP, ICONE_STEP

RAIZ_WEB = Path(__file__).resolve().parent
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Pesquisa de Preços PLASEG", version="0.2.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("WEB_SECRET_KEY") or os.urandom(32).hex())
app.mount("/static", StaticFiles(directory=RAIZ_WEB / "static"), name="static")

# ── Superfície JSON (ex-`api/app.py`, Fase 4) ────────────────────────────────────────
# Mesmos `services/` que o HTML consome; o que muda é só a representação e a autenticação.
for _router in (router_runs.router, router_config.router, router_notifications.router):
    app.include_router(_router, prefix="/api", dependencies=[Depends(exigir_token)])


def _erro_json(codigo: str, mensagem: str, status_code: int) -> JSONResponse:
    """Formato de erro estável de docs/06_API_E_WEB.md §3.3: `{erro: {codigo, mensagem}}`,
    com `codigo` legível para o cliente decidir o que fazer sem parsear texto livre."""
    return JSONResponse(status_code=status_code,
                        content={"erro": {"codigo": codigo, "mensagem": mensagem}})


# Os handlers são registrados na app inteira, mas as rotas HTML já tratam essas exceções por
# dentro (`_redirecionar_com_erro`) — na prática só as rotas `/api` chegam aqui.
_ERROS_JSON: tuple[tuple[type[Exception], str, int], ...] = (
    (ExecucaoEmAndamento, "EXECUCAO_EM_ANDAMENTO", 409),
    (DependenciaNaoSatisfeita, "DEPENDENCIA_NAO_SATISFEITA", 422),
    (RefazerSemConfirmacao, "CONFIRMACAO_NECESSARIA", 422),
    (RunInexistente, "NAO_ENCONTRADO", 404),
)

for _excecao, _codigo, _status in _ERROS_JSON:
    def _handler(request: Request, exc: Exception, _c=_codigo, _s=_status):
        return _erro_json(_c, str(exc), _s)

    app.add_exception_handler(_excecao, _handler)


@app.exception_handler(KeyError)
async def _tratar_chave_invalida(request: Request, exc: KeyError):
    return _erro_json("NAO_ENCONTRADO", str(exc).strip("'\""), 404)


@app.get("/api/health")
def health():
    """Sem autenticação — é o endpoint que um monitor externo bate antes de saber se vale a
    pena mandar um token."""
    ok, mensagem = db.is_available()
    return {"status": "ok" if ok else "erro", "banco": mensagem, "version": app.version}


@app.get("/api/providers/status")
def providers_status(_: None = Depends(exigir_token)):
    return {"provedores": service.listar_provedores()}


templates = Jinja2Templates(directory=RAIZ_WEB / "templates")
templates.env.globals["icone_step"] = ICONE_STEP
templates.env.globals["classe_step"] = CLASSE_STEP


def _render(request: Request, name: str, contexto: dict[str, Any] | None = None, status_code: int = 200):
    ctx = {"usuario": request.session.get("usuario"), "erro": request.query_params.get("erro")}
    ctx.update(contexto or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


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
def fazer_login(request: Request, user: str = Form(...), password: str = Form("")):
    if not auth.tentar_login(request, password, user):
        return RedirectResponse("/login?erro=password%20inv%C3%A1lida", status_code=303)
    return RedirectResponse("/runs", status_code=303)


@app.post("/logout")
def fazer_logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)


# ── Runs ──────────────────────────────────────────────────────────────────────────────

@app.get("/runs")
def lista_runs(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "runs_list.html", {"runs": service.listar_runs()})


@app.post("/runs")
def criar_run(request: Request, label: str = Form(...), mode: str = Form("assisted"),
             cost_cap_usd: str = Form(""), user: str = Depends(auth.exigir_login)):
    run_id = service.criar_run(label, mode=mode,
                               cost_cap_usd=float(cost_cap_usd) if cost_cap_usd else None,
                               created_by=user)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def hub_run(request: Request, run_id: int, user: str = Depends(auth.exigir_login)):
    run = service.obter_run(run_id)
    if run is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "run_hub.html", {"run": run})


@app.get("/runs/{run_id}/graph")
def fragmento_grafo(request: Request, run_id: int, user: str = Depends(auth.exigir_login)):
    run = service.obter_run(run_id)
    if run is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "_graph.html", {"run": run})


@app.post("/runs/{run_id}/abort")
def abortar_run(request: Request, run_id: int, user: str = Depends(auth.exigir_login)):
    try:
        service.abortar_run(run_id)
    except RunInexistente as exc:
        return _redirecionar_com_erro("/runs", exc)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── Etapa ─────────────────────────────────────────────────────────────────────────────

def _contexto_etapa(run_id: int, key: str) -> dict[str, Any] | None:
    from pesquisa_precos.steps import registry

    run = service.obter_run(run_id)
    if run is None:
        return None
    detalhe = service.detalhe_etapa(run_id, key)
    estimativa, estimativa_erro = None, None
    try:
        estimativa = service.estimativa_etapa(key)
    except Exception as exc:  # noqa: BLE001 — estimativa é auxiliar; a tela não pode cair por causa dela
        estimativa_erro = str(exc)
    return {
        "run": run, "key": key, "definicao": registry.obter(key), "detalhe": detalhe,
        "dependentes": registry.dependentes(key),
        "estimativa": estimativa, "estimativa_erro": estimativa_erro,
        "erros": service.erros(run_id, step=key),
        "logs": list(reversed(service.logs(run_id, step=key, limite=200))),
    }


@app.get("/runs/{run_id}/steps/{key}")
def tela_etapa(request: Request, run_id: int, key: str, user: str = Depends(auth.exigir_login)):
    try:
        ctx = _contexto_etapa(run_id, key)
    except KeyError as exc:
        return _redirecionar_com_erro(f"/runs/{run_id}", exc)
    if ctx is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "step.html", ctx)


@app.get("/runs/{run_id}/steps/{key}/fragment")
def fragmento_etapa(request: Request, run_id: int, key: str, user: str = Depends(auth.exigir_login)):
    ctx = _contexto_etapa(run_id, key)
    if ctx is None:
        return _redirecionar_com_erro("/runs", RunInexistente(f"run {run_id} não existe"))
    return _render(request, "_step_progress.html", ctx)


@app.get("/runs/{run_id}/steps/{key}/log/stream")
def log_stream_etapa(run_id: int, key: str, request: Request, user: str = Depends(auth.exigir_login)):
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    async def eventos():
        ultimo_id = 0
        while True:
            recentes = sorted(service.logs(run_id, step=key, limite=50), key=lambda l: l["id"])
            for linha in recentes:
                if linha["id"] > ultimo_id:
                    ultimo_id = linha["id"]
                    yield f"data: {json.dumps(linha, default=str, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(eventos(), media_type="text/event-stream")


@app.post("/runs/{run_id}/steps/{key}/run")
def executar_etapa(request: Request, run_id: int, key: str, action: str = Form("update"),
                   confirm: bool = Form(False), user: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/steps/{key}"
    try:
        service.executar_etapa(run_id, key, action=action, confirm=confirm)
    except (RunInexistente, DependenciaNaoSatisfeita, ExecucaoEmAndamento,
            RefazerSemConfirmacao) as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/steps/{key}/cancel")
def cancelar_etapa(request: Request, run_id: int, key: str, user: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/steps/{key}"
    try:
        service.cancelar_etapa(run_id, key)
    except RunInexistente as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/steps/{key}/approve")
def aprovar_etapa(request: Request, run_id: int, key: str, params_override: str = Form("{}"),
                  user: str = Depends(auth.exigir_login)):
    import json

    voltar = f"/runs/{run_id}/steps/{key}"
    try:
        override = json.loads(params_override) if params_override.strip() else {}
    except json.JSONDecodeError as exc:
        return _redirecionar_com_erro(voltar, ValueError(f"params_override não é JSON válido: {exc}"))
    try:
        service.aprovar_etapa(run_id, key, approved_by=user, params_override=override)
        service.executar_etapa(run_id, key, action="update")
    except (RunInexistente, DependenciaNaoSatisfeita, ExecucaoEmAndamento) as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(voltar, status_code=303)


@app.post("/runs/{run_id}/steps/{key}/skip")
def pular_etapa(request: Request, run_id: int, key: str, reason: str = Form(""),
                user: str = Depends(auth.exigir_login)):
    voltar = f"/runs/{run_id}/steps/{key}"
    try:
        service.pular_etapa(run_id, key, reason=reason or f"pulada por {user}")
    except RunInexistente as exc:
        return _redirecionar_com_erro(voltar, exc)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── Custo e exports ───────────────────────────────────────────────────────────────────

@app.get("/cost")
def dashboard_custo(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "cost.html", {"resumo": service.custo_resumo()})


@app.get("/exports")
def tela_exports(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "exports.html", {"exports": service.listar_exports()})


@app.get("/exports/{export_id}/download")
def baixar_export(export_id: int, user: str = Depends(auth.exigir_login)):
    """Serve o XLSX de `export.conteudo` (ADR-018 §2) — não existe arquivo em disco."""
    export, conteudo, name = service.conteudo_export(export_id)
    if export is None:
        return RedirectResponse("/exports?erro=export%20n%C3%A3o%20encontrado", status_code=303)
    if conteudo is None:
        return RedirectResponse(
            "/exports?erro=export%20anterior%20%C3%A0%20Fase%2010%20-%20o%20arquivo%20ficou%20"
            "em%20data%2F%2C%20fora%20do%20banco", status_code=303)
    return Response(
        conteudo, media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Providers: saúde + CRUD (Fase 13 / Fase 14 bloco 2, ADR-022) ────────────────────
#
# A tela era só leitura: sondava as capacidades e mostrava o resultado. Com a Fase 14 ela vira
# a superfície onde se CONFIGURA quem atende cada capacidade — modelo, base_url e chave de API
# deixam de exigir editar `.env` e reiniciar o servidor.
#
# A chave de API é write-only em todo este bloco: entra por `Form`, sai cifrada para o banco, e
# o que volta para o template é `has_api_key`/`api_key_last4`. Nenhuma rota daqui devolve
# segredo em claro, e `tests/test_segredo.py::test_so_o_resolver_decifra` guarda a regra.

_FORM_CAPACIDADES = Form(default=[])


def _contexto_provedores(**extra: Any) -> dict[str, Any]:
    return {
        "resultados": service.saude_provedores(),
        "provedores": service_providers.listar(),
        "capabilities": service_providers.CAPACIDADES,
        "chave_mestra": service_providers.diagnostico_chave_mestra(),
        "a_recifrar": service_providers.keys_a_recifrar(),
        "editando": None, **extra}


def _num(valor: str) -> float | None:
    """Campo numérico de formulário HTML chega como string, e vazio é `''`, não `None`."""
    try:
        return float(valor) if (valor or "").strip() else None
    except ValueError:
        return None


@app.get("/providers")
def tela_provedores(request: Request, editar: str | None = None,
                    user: str = Depends(auth.exigir_login)):
    editando = service_providers.obter(editar) if editar else None
    return _render(request, "providers.html", _contexto_provedores(editando=editando))


@app.post("/providers")
def salvar_provedor(request: Request, name: str = Form(""), base_url: str = Form(""),
                    capabilities: list[str] = _FORM_CAPACIDADES, default_model: str = Form(""),
                    batch_size: str = Form(""), rpm_limit: str = Form(""),
                    cost_in_per_mtok: str = Form(""), cost_out_per_mtok: str = Form(""),
                    cost_usd_per_call: str = Form(""),
                    active: str = Form("on"), api_key: str = Form(""),
                    user: str = Depends(auth.exigir_login)):
    try:
        service_providers.salvar(
            name, capabilities, base_url, default_model=default_model or None,
            batch_size=int(batch_size) if batch_size.strip() else None,
            rpm_limit=int(rpm_limit) if rpm_limit.strip() else None,
            cost_in_per_mtok=_num(cost_in_per_mtok),
            cost_out_per_mtok=_num(cost_out_per_mtok),
            cost_usd_per_call=_num(cost_usd_per_call),
            active=active == "on", api_key=api_key or None)
    except (InvalidProvider, ChaveMestraAusente) as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/{name}/key")
def gravar_chave_provedor(request: Request, name: str, api_key: str = Form(""),
                          user: str = Depends(auth.exigir_login)):
    try:
        service_providers.gravar_api_key(name, api_key)
    except (InvalidProvider, ProvedorInexistente, ChaveMestraAusente) as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/{name}/key/clear")
def limpar_chave_provedor(request: Request, name: str,
                          user: str = Depends(auth.exigir_login)):
    try:
        service_providers.limpar_api_key(name)
    except ProvedorInexistente as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/{name}/active")
def alternar_ativo_provedor(request: Request, name: str, active: str = Form("on"),
                            user: str = Depends(auth.exigir_login)):
    try:
        service_providers.definir_ativo(name, active == "on")
    except (ProvedorInexistente, InvalidProvider) as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/{name}/test")
def testar_provedor(request: Request, name: str, user: str = Depends(auth.exigir_login)):
    """Sondagem HTTP leve — não gasta e não dispara etapa, então não fere a regra nº 1 do
    CLAUDE.md ("quem roda a pipeline é o usuário")."""
    try:
        service_providers.testar(name)
    except ProvedorInexistente as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/capabilities")
def apontar_capacidade(request: Request, capability: str = Form(""),
                       provider: str = Form(""), model: str = Form(""),
                       fallback: str = Form(""),
                       user: str = Depends(auth.exigir_login)):
    try:
        service_providers.apontar(capability, provider, model or None, fallback or None)
    except (InvalidProvider, ProvedorInexistente, FallbackProibido) as exc:
        return _redirecionar_com_erro("/providers", exc)
    return RedirectResponse("/providers", status_code=303)


@app.post("/providers/recrypt")
def recifrar_chaves(request: Request, user: str = Depends(auth.exigir_login)):
    """Rotação de `APP_SECRET_KEY`: re-cifra o que ficou na chave anterior (ADR-022)."""
    try:
        resultado = service_providers.recifrar_tudo()
    except ChaveMestraAusente as exc:
        return _redirecionar_com_erro("/providers", exc)
    if resultado["falharam"]:
        # Falha parcial é informação, não erro: o resto foi re-cifrado, e estas linhas não
        # decifram com nenhuma chave disponível — a saída é recadastrar a chave delas.
        return _redirecionar_com_erro("/providers", RuntimeError(
            f"{resultado['recifradas']} re-cifradas. NÃO foi possível decifrar: "
            f"{', '.join(resultado['falharam'])} — recadastre a key desses provedores "
            f"(defina APP_SECRET_KEY_ANTIGA se a key anterior ainda existir)."))
    return RedirectResponse("/providers", status_code=303)


# ── Diff entre runs (Fase 9) ─────────────────────────────────────────────────────────

@app.get("/diff")
def tela_diff(request: Request, run_a: int | None = None, run_b: int | None = None,
             user: str = Depends(auth.exigir_login)):
    runs = service.listar_runs()
    ctx: dict[str, Any] = {"runs": runs, "run_a": run_a, "run_b": run_b, "diff": None}
    if run_a is not None and run_b is not None:
        try:
            ctx["diff"] = service_diff.diff_runs(run_a, run_b)
        except RunSemRankingError as exc:
            ctx["erro_diff"] = str(exc)
    return _render(request, "diff.html", ctx)


# ── Recalibração de thresholds (Fase 9) ─────────────────────────────────────────────

@app.get("/recalibrate")
def tela_recalibrar(request: Request, t_aceita: float | None = None,
                    t_rejeita: float | None = None, user: str = Depends(auth.exigir_login)):
    ctx: dict[str, Any] = {"t_aceita": t_aceita if t_aceita is not None else 0.80,
                           "t_rejeita": t_rejeita if t_rejeita is not None else 0.30,
                           "resultado": None, "resultado_erro": None}
    if t_aceita is not None and t_rejeita is not None:
        try:
            ctx["resultado"] = service_config.recalibrar_threshold(t_aceita, t_rejeita)
        except Exception as exc:  # noqa: BLE001 — banco vazio/indisponível não pode derrubar a tela
            ctx["resultado_erro"] = str(exc)
    return _render(request, "recalibrate.html", ctx)


# ── Configuração (Fase 6) ────────────────────────────────────────────────────────────

@app.get("/config")
def tela_config(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "config.html", {
        "versoes": service_config.listar_config_versoes(),
        "schema": service_config.schema_parametros(), "diff": None})


@app.get("/config/diff")
def diff_config(request: Request, a: int, b: int, user: str = Depends(auth.exigir_login)):
    try:
        diff = service_config.diff_config_versoes(a, b)
    except ConfigVersaoInexistente as exc:
        return _redirecionar_com_erro("/config", exc)
    return _render(request, "config.html", {
        "versoes": service_config.listar_config_versoes(),
        "schema": service_config.schema_parametros(), "diff": diff})


@app.post("/config")
async def criar_config(request: Request, label: str = Form(...), notes: str = Form(""),
                       user: str = Depends(auth.exigir_login)):
    forma = await request.form()
    valores = {}
    for key, valor in forma.multi_items():
        if key.startswith("campo__") and str(valor).strip():
            valores[key.removeprefix("campo__")] = str(valor).strip()
    service_config.criar_config_versao(label, valores, created_by=user, notes=notes or None)
    return RedirectResponse("/config", status_code=303)


# ── Prompts (Fase 6) ──────────────────────────────────────────────────────────────────

@app.get("/prompts")
def tela_prompts(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "prompts.html", {
        "prompts": service_prompts.listar_prompts(), "diff": None, "open": None})


@app.get("/prompts/{name}/{version}")
def diff_prompt(request: Request, name: str, version: int, user: str = Depends(auth.exigir_login)):
    try:
        versoes = service_prompts.versoes_prompt(name)
    except PromptInexistente as exc:
        return _redirecionar_com_erro("/prompts", exc)
    ativa = next((v["version"] for v in versoes if v["ativa"]), versoes[0]["version"])
    diff = service_prompts.diff_versoes(name, ativa, version) if ativa != version else None
    return _render(request, "prompts.html", {
        "prompts": service_prompts.listar_prompts(), "diff": diff, "open": name})


@app.post("/prompts/{name}/versions")
def criar_versao_prompt(request: Request, name: str, template: str = Form(...),
                        notes: str = Form(""), user: str = Depends(auth.exigir_login)):
    service_prompts.criar_versao(name, template, created_by=user, notes=notes or None)
    return RedirectResponse("/prompts", status_code=303)


@app.post("/prompts/{name}/{version}/activate")
def ativar_versao_prompt(request: Request, name: str, version: int,
                         user: str = Depends(auth.exigir_login)):
    try:
        service_prompts.ativar_versao(name, version)
    except PromptInexistente as exc:
        return _redirecionar_com_erro("/prompts", exc)
    return RedirectResponse("/prompts", status_code=303)


# ── Destinatários de notificação (Fase 9) ────────────────────────────────────────────

@app.get("/notifications")
def tela_notificacoes(request: Request, user: str = Depends(auth.exigir_login)):
    return _render(request, "notifications.html", {
        "destinatarios": service_recipients.listar_destinatarios(),
        "editando": None})


@app.get("/notifications/{recipient_id}/edit")
def editar_form_destinatario(request: Request, recipient_id: int,
                             user: str = Depends(auth.exigir_login)):
    destinatario = service_recipients.obter_destinatario(recipient_id)
    if destinatario is None:
        return _redirecionar_com_erro(
            "/notifications", DestinatarioInexistente(f"destinatário {recipient_id} não existe"))
    return _render(request, "notifications.html", {
        "destinatarios": service_recipients.listar_destinatarios(),
        "editando": destinatario})


@app.post("/notifications")
def criar_destinatario(request: Request, name: str = Form(""), email: str = Form(""),
                       user: str = Depends(auth.exigir_login)):
    try:
        service_recipients.criar_destinatario(name or None, email or None)
    except DestinatarioSemCanal as exc:
        return _redirecionar_com_erro("/notifications", exc)
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{recipient_id}")
def editar_destinatario(request: Request, recipient_id: int, name: str = Form(""),
                        email: str = Form(""), user: str = Depends(auth.exigir_login)):
    try:
        service_recipients.editar_destinatario(recipient_id, name or None, email or None)
    except (DestinatarioSemCanal, DestinatarioInexistente) as exc:
        return _redirecionar_com_erro("/notifications", exc)
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{recipient_id}/deactivate")
def desativar_destinatario(request: Request, recipient_id: int,
                           user: str = Depends(auth.exigir_login)):
    try:
        service_recipients.desativar_destinatario(recipient_id)
    except DestinatarioInexistente as exc:
        return _redirecionar_com_erro("/notifications", exc)
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{recipient_id}/activate")
def ativar_destinatario(request: Request, recipient_id: int,
                        user: str = Depends(auth.exigir_login)):
    try:
        service_recipients.ativar_destinatario(recipient_id)
    except DestinatarioInexistente as exc:
        return _redirecionar_com_erro("/notifications", exc)
    return RedirectResponse("/notifications", status_code=303)
