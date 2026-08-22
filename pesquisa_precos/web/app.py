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
from pesquisa_precos.api.routers import notificacoes as router_notificacoes
from pesquisa_precos.api.routers import runs as router_runs
from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.segredo import ChaveMestraAusente, SegredoInvalido
from pesquisa_precos.services import config as servico_config
from pesquisa_precos.services import diff as servico_diff
from pesquisa_precos.services import execucao as servico
from pesquisa_precos.services import notificacao_destinatarios as servico_destinatarios
from pesquisa_precos.services import prompts as servico_prompts
from pesquisa_precos.services import provedores as servico_provedores
from pesquisa_precos.services.config import ConfigVersaoInexistente
from pesquisa_precos.services.diff import RunSemRankingError
from pesquisa_precos.services.execucao import (
    DependenciaNaoSatisfeita,
    ExecucaoEmAndamento,
    RefazerSemConfirmacao,
    RunInexistente,
)
from pesquisa_precos.services.notificacao_destinatarios import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)
from pesquisa_precos.services.prompts import PromptInexistente
from pesquisa_precos.services.provedores import (
    FallbackProibido,
    ProvedorInexistente,
    ProvedorInvalido,
)
from pesquisa_precos.web import auth
from pesquisa_precos.web.estado import CLASSE_ETAPA, ICONE_ETAPA

RAIZ_WEB = Path(__file__).resolve().parent
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Pesquisa de Preços PLASEG", version="0.2.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("WEB_SECRET_KEY") or os.urandom(32).hex())
app.mount("/static", StaticFiles(directory=RAIZ_WEB / "static"), name="static")

# ── Superfície JSON (ex-`api/app.py`, Fase 4) ────────────────────────────────────────
# Mesmos `services/` que o HTML consome; o que muda é só a representação e a autenticação.
for _router in (router_runs.router, router_config.router, router_notificacoes.router):
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
    ok, mensagem = db.esta_disponivel()
    return {"status": "ok" if ok else "erro", "banco": mensagem, "versao": app.version}


@app.get("/api/providers/status")
def providers_status(_: None = Depends(exigir_token)):
    return {"provedores": servico.listar_provedores()}


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
        "dependentes": registry.dependentes(chave),
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
    """Serve o XLSX de `export.conteudo` (ADR-018 §2) — não existe arquivo em disco."""
    export, conteudo, nome = servico.conteudo_export(export_id)
    if export is None:
        return RedirectResponse("/exports?erro=export%20n%C3%A3o%20encontrado", status_code=303)
    if conteudo is None:
        return RedirectResponse(
            "/exports?erro=export%20anterior%20%C3%A0%20Fase%2010%20-%20o%20arquivo%20ficou%20"
            "em%20data%2F%2C%20fora%20do%20banco", status_code=303)
    return Response(
        conteudo, media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'})


# ── Provedores: saúde + CRUD (Fase 13 / Fase 14 bloco 2, ADR-022) ────────────────────
#
# A tela era só leitura: sondava as capacidades e mostrava o resultado. Com a Fase 14 ela vira
# a superfície onde se CONFIGURA quem atende cada capacidade — modelo, base_url e chave de API
# deixam de exigir editar `.env` e reiniciar o servidor.
#
# A chave de API é write-only em todo este bloco: entra por `Form`, sai cifrada para o banco, e
# o que volta para o template é `tem_api_key`/`api_key_last4`. Nenhuma rota daqui devolve
# segredo em claro, e `tests/test_segredo.py::test_so_o_resolver_decifra` guarda a regra.

_FORM_CAPACIDADES = Form(default=[])


def _contexto_provedores(**extra: Any) -> dict[str, Any]:
    return {
        "resultados": servico.saude_provedores(),
        "provedores": servico_provedores.listar(),
        "capacidades": servico_provedores.CAPACIDADES,
        "chave_mestra": servico_provedores.diagnostico_chave_mestra(),
        "a_recifrar": servico_provedores.chaves_a_recifrar(),
        "editando": None, **extra}


def _num(valor: str) -> float | None:
    """Campo numérico de formulário HTML chega como string, e vazio é `''`, não `None`."""
    try:
        return float(valor) if (valor or "").strip() else None
    except ValueError:
        return None


@app.get("/provedores")
def tela_provedores(request: Request, editar: str | None = None,
                    usuario: str = Depends(auth.exigir_login)):
    editando = servico_provedores.obter(editar) if editar else None
    return _render(request, "provedores.html", _contexto_provedores(editando=editando))


@app.post("/provedores")
def salvar_provedor(request: Request, nome: str = Form(""), base_url: str = Form(""),
                    capacidades: list[str] = _FORM_CAPACIDADES, modelo_padrao: str = Form(""),
                    batch_size: str = Form(""), rpm_limite: str = Form(""),
                    custo_in_por_mtok: str = Form(""), custo_out_por_mtok: str = Form(""),
                    custo_usd_chamada: str = Form(""),
                    ativo: str = Form("on"), api_key: str = Form(""),
                    usuario: str = Depends(auth.exigir_login)):
    try:
        servico_provedores.salvar(
            nome, capacidades, base_url, modelo_padrao=modelo_padrao or None,
            batch_size=int(batch_size) if batch_size.strip() else None,
            rpm_limite=int(rpm_limite) if rpm_limite.strip() else None,
            custo_in_por_mtok=_num(custo_in_por_mtok),
            custo_out_por_mtok=_num(custo_out_por_mtok),
            custo_usd_chamada=_num(custo_usd_chamada),
            ativo=ativo == "on", api_key=api_key or None)
    except (ProvedorInvalido, ChaveMestraAusente) as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/{nome}/chave")
def gravar_chave_provedor(request: Request, nome: str, api_key: str = Form(""),
                          usuario: str = Depends(auth.exigir_login)):
    try:
        servico_provedores.gravar_api_key(nome, api_key)
    except (ProvedorInvalido, ProvedorInexistente, ChaveMestraAusente) as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/{nome}/chave/limpar")
def limpar_chave_provedor(request: Request, nome: str,
                          usuario: str = Depends(auth.exigir_login)):
    try:
        servico_provedores.limpar_api_key(nome)
    except ProvedorInexistente as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/{nome}/ativo")
def alternar_ativo_provedor(request: Request, nome: str, ativo: str = Form("on"),
                            usuario: str = Depends(auth.exigir_login)):
    try:
        servico_provedores.definir_ativo(nome, ativo == "on")
    except (ProvedorInexistente, ProvedorInvalido) as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/{nome}/testar")
def testar_provedor(request: Request, nome: str, usuario: str = Depends(auth.exigir_login)):
    """Sondagem HTTP leve — não gasta e não dispara etapa, então não fere a regra nº 1 do
    CLAUDE.md ("quem roda a pipeline é o usuário")."""
    try:
        servico_provedores.testar(nome)
    except ProvedorInexistente as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/capacidades")
def apontar_capacidade(request: Request, capacidade: str = Form(""),
                       provedor: str = Form(""), modelo: str = Form(""),
                       fallback: str = Form(""),
                       usuario: str = Depends(auth.exigir_login)):
    try:
        servico_provedores.apontar(capacidade, provedor, modelo or None, fallback or None)
    except (ProvedorInvalido, ProvedorInexistente, FallbackProibido) as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


@app.post("/provedores/recifrar")
def recifrar_chaves(request: Request, usuario: str = Depends(auth.exigir_login)):
    """Rotação de `APP_SECRET_KEY`: re-cifra o que ficou na chave anterior (ADR-022)."""
    try:
        servico_provedores.recifrar_tudo()
    except (ChaveMestraAusente, SegredoInvalido) as exc:
        return _redirecionar_com_erro("/provedores", exc)
    return RedirectResponse("/provedores", status_code=303)


# ── Diff entre runs (Fase 9) ─────────────────────────────────────────────────────────

@app.get("/diff")
def tela_diff(request: Request, run_a: int | None = None, run_b: int | None = None,
             usuario: str = Depends(auth.exigir_login)):
    runs = servico.listar_runs()
    ctx: dict[str, Any] = {"runs": runs, "run_a": run_a, "run_b": run_b, "diff": None}
    if run_a is not None and run_b is not None:
        try:
            ctx["diff"] = servico_diff.diff_runs(run_a, run_b)
        except RunSemRankingError as exc:
            ctx["erro_diff"] = str(exc)
    return _render(request, "diff.html", ctx)


# ── Recalibração de thresholds (Fase 9) ─────────────────────────────────────────────

@app.get("/recalibrar")
def tela_recalibrar(request: Request, t_aceita: float | None = None,
                    t_rejeita: float | None = None, usuario: str = Depends(auth.exigir_login)):
    ctx: dict[str, Any] = {"t_aceita": t_aceita if t_aceita is not None else 0.80,
                           "t_rejeita": t_rejeita if t_rejeita is not None else 0.30,
                           "resultado": None, "resultado_erro": None}
    if t_aceita is not None and t_rejeita is not None:
        try:
            ctx["resultado"] = servico_config.recalibrar_threshold(t_aceita, t_rejeita)
        except Exception as exc:  # noqa: BLE001 — banco vazio/indisponível não pode derrubar a tela
            ctx["resultado_erro"] = str(exc)
    return _render(request, "recalibrar.html", ctx)


# ── Configuração (Fase 6) ────────────────────────────────────────────────────────────

@app.get("/config")
def tela_config(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "config.html", {
        "versoes": servico_config.listar_config_versoes(),
        "schema": servico_config.schema_parametros(), "diff": None})


@app.get("/config/diff")
def diff_config(request: Request, a: int, b: int, usuario: str = Depends(auth.exigir_login)):
    try:
        diff = servico_config.diff_config_versoes(a, b)
    except ConfigVersaoInexistente as exc:
        return _redirecionar_com_erro("/config", exc)
    return _render(request, "config.html", {
        "versoes": servico_config.listar_config_versoes(),
        "schema": servico_config.schema_parametros(), "diff": diff})


@app.post("/config")
async def criar_config(request: Request, rotulo: str = Form(...), notas: str = Form(""),
                       usuario: str = Depends(auth.exigir_login)):
    forma = await request.form()
    valores = {}
    for chave, valor in forma.multi_items():
        if chave.startswith("campo__") and str(valor).strip():
            valores[chave.removeprefix("campo__")] = str(valor).strip()
    servico_config.criar_config_versao(rotulo, valores, criado_por=usuario, notas=notas or None)
    return RedirectResponse("/config", status_code=303)


# ── Prompts (Fase 6) ──────────────────────────────────────────────────────────────────

@app.get("/prompts")
def tela_prompts(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "prompts.html", {
        "prompts": servico_prompts.listar_prompts(), "diff": None, "aberto": None})


@app.get("/prompts/{nome}/{versao}")
def diff_prompt(request: Request, nome: str, versao: int, usuario: str = Depends(auth.exigir_login)):
    try:
        versoes = servico_prompts.versoes_prompt(nome)
    except PromptInexistente as exc:
        return _redirecionar_com_erro("/prompts", exc)
    ativa = next((v["versao"] for v in versoes if v["ativa"]), versoes[0]["versao"])
    diff = servico_prompts.diff_versoes(nome, ativa, versao) if ativa != versao else None
    return _render(request, "prompts.html", {
        "prompts": servico_prompts.listar_prompts(), "diff": diff, "aberto": nome})


@app.post("/prompts/{nome}/versoes")
def criar_versao_prompt(request: Request, nome: str, template: str = Form(...),
                        notas: str = Form(""), usuario: str = Depends(auth.exigir_login)):
    servico_prompts.criar_versao(nome, template, criado_por=usuario, notas=notas or None)
    return RedirectResponse("/prompts", status_code=303)


@app.post("/prompts/{nome}/{versao}/ativar")
def ativar_versao_prompt(request: Request, nome: str, versao: int,
                         usuario: str = Depends(auth.exigir_login)):
    try:
        servico_prompts.ativar_versao(nome, versao)
    except PromptInexistente as exc:
        return _redirecionar_com_erro("/prompts", exc)
    return RedirectResponse("/prompts", status_code=303)


# ── Destinatários de notificação (Fase 9) ────────────────────────────────────────────

@app.get("/notificacoes")
def tela_notificacoes(request: Request, usuario: str = Depends(auth.exigir_login)):
    return _render(request, "notificacoes.html", {
        "destinatarios": servico_destinatarios.listar_destinatarios(),
        "editando": None})


@app.get("/notificacoes/{destinatario_id}/editar")
def editar_form_destinatario(request: Request, destinatario_id: int,
                             usuario: str = Depends(auth.exigir_login)):
    destinatario = servico_destinatarios.obter_destinatario(destinatario_id)
    if destinatario is None:
        return _redirecionar_com_erro(
            "/notificacoes", DestinatarioInexistente(f"destinatário {destinatario_id} não existe"))
    return _render(request, "notificacoes.html", {
        "destinatarios": servico_destinatarios.listar_destinatarios(),
        "editando": destinatario})


@app.post("/notificacoes")
def criar_destinatario(request: Request, nome: str = Form(""), email: str = Form(""),
                       usuario: str = Depends(auth.exigir_login)):
    try:
        servico_destinatarios.criar_destinatario(nome or None, email or None)
    except DestinatarioSemCanal as exc:
        return _redirecionar_com_erro("/notificacoes", exc)
    return RedirectResponse("/notificacoes", status_code=303)


@app.post("/notificacoes/{destinatario_id}")
def editar_destinatario(request: Request, destinatario_id: int, nome: str = Form(""),
                        email: str = Form(""), usuario: str = Depends(auth.exigir_login)):
    try:
        servico_destinatarios.editar_destinatario(destinatario_id, nome or None, email or None)
    except (DestinatarioSemCanal, DestinatarioInexistente) as exc:
        return _redirecionar_com_erro("/notificacoes", exc)
    return RedirectResponse("/notificacoes", status_code=303)


@app.post("/notificacoes/{destinatario_id}/desativar")
def desativar_destinatario(request: Request, destinatario_id: int,
                           usuario: str = Depends(auth.exigir_login)):
    try:
        servico_destinatarios.desativar_destinatario(destinatario_id)
    except DestinatarioInexistente as exc:
        return _redirecionar_com_erro("/notificacoes", exc)
    return RedirectResponse("/notificacoes", status_code=303)


@app.post("/notificacoes/{destinatario_id}/ativar")
def ativar_destinatario(request: Request, destinatario_id: int,
                        usuario: str = Depends(auth.exigir_login)):
    try:
        servico_destinatarios.ativar_destinatario(destinatario_id)
    except DestinatarioInexistente as exc:
        return _redirecionar_com_erro("/notificacoes", exc)
    return RedirectResponse("/notificacoes", status_code=303)
