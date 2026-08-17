"""
Rotas `/api/config/*` e `/api/prompts/*` — parametrização e prompts sem deploy (Fase 6,
docs/04_FASES.md, docs/06_API_E_WEB.md §3.2/§4.5). Toda rota chama `services/`, nunca
`db/repos` direto (docs/01_ARQUITETURA.md §7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pesquisa_precos.api.schemas import CriarConfigVersaoBody, CriarPromptVersaoBody
from pesquisa_precos.services import config as servico_config
from pesquisa_precos.services import prompts as servico_prompts
from pesquisa_precos.services.config import ConfigVersaoInexistente
from pesquisa_precos.services.prompts import PromptInexistente

router = APIRouter(tags=["config"])


@router.get("/config/versoes")
def listar_config_versoes():
    return servico_config.listar_config_versoes()


@router.get("/config/versoes/{config_versao_id}")
def obter_config_versao(config_versao_id: int):
    versao = servico_config.obter_config_versao(config_versao_id)
    if versao is None:
        raise HTTPException(404, f"config_versao {config_versao_id} não existe")
    return versao


@router.post("/config/versoes", status_code=201)
def criar_config_versao(body: CriarConfigVersaoBody):
    config_versao_id = servico_config.criar_config_versao(
        body.rotulo, body.valores, criado_por=body.criado_por, notas=body.notas)
    return servico_config.obter_config_versao(config_versao_id)


@router.get("/config/versoes/{config_versao_id}/diff/{outro_id}")
def diff_config_versoes(config_versao_id: int, outro_id: int):
    try:
        return servico_config.diff_config_versoes(config_versao_id, outro_id)
    except ConfigVersaoInexistente as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/config/schema")
def schema_parametros():
    """Campos do `Params` Pydantic por etapa — fonte do formulário de configuração
    (docs/06_API_E_WEB.md §4.5)."""
    return servico_config.schema_parametros()


@router.get("/config/recalibrar")
def recalibrar_threshold(t_aceita: float, t_rejeita: float, limite_amostra: int = 500):
    """Fase 9, item 6: precisão/recall de thresholds CANDIDATOS contra `rotulo`, antes de
    o operador gravar uma `config_versao` nova com eles."""
    return servico_config.recalibrar_threshold(t_aceita, t_rejeita, limite_amostra=limite_amostra)


@router.get("/prompts")
def listar_prompts():
    return servico_prompts.listar_prompts()


@router.get("/prompts/{nome}/versoes")
def versoes_prompt(nome: str):
    try:
        return servico_prompts.versoes_prompt(nome)
    except PromptInexistente as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/prompts/{nome}/versoes", status_code=201)
def criar_versao_prompt(nome: str, body: CriarPromptVersaoBody):
    versao_id = servico_prompts.criar_versao(nome, body.template, criado_por=body.criado_por,
                                             notas=body.notas)
    return {"id": versao_id, "prompt_nome": nome}


@router.post("/prompts/{nome}/versoes/{versao}/ativar")
def ativar_versao_prompt(nome: str, versao: int):
    try:
        servico_prompts.ativar_versao(nome, versao)
    except PromptInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"prompt_nome": nome, "versao_ativa": versao}


@router.get("/prompts/{nome}/diff/{versao_a}/{versao_b}")
def diff_versoes_prompt(nome: str, versao_a: int, versao_b: int):
    try:
        return servico_prompts.diff_versoes(nome, versao_a, versao_b)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc
