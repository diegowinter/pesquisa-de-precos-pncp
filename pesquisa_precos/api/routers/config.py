"""
Rotas `/api/config/*` e `/api/prompts/*` — parametrização e prompts sem deploy (Fase 6,
docs/04_FASES.md, docs/06_API_E_WEB.md §3.2/§4.5). Toda rota chama `services/`, nunca
`db/repos` direto (docs/01_ARQUITETURA.md §7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pesquisa_precos.api.schemas import CriarConfigVersaoBody, CriarPromptVersaoBody
from pesquisa_precos.services import config as service_config
from pesquisa_precos.services import prompts as service_prompts
from pesquisa_precos.services.config import ConfigVersaoInexistente
from pesquisa_precos.services.prompts import PromptInexistente

router = APIRouter(tags=["config"])


@router.get("/config/versions")
def listar_config_versoes():
    return service_config.listar_config_versoes()


@router.get("/config/versions/{config_version_id}")
def obter_config_versao(config_version_id: int):
    version = service_config.obter_config_versao(config_version_id)
    if version is None:
        raise HTTPException(404, f"config_version {config_version_id} não existe")
    return version


@router.post("/config/versions", status_code=201)
def criar_config_versao(body: CriarConfigVersaoBody):
    config_version_id = service_config.criar_config_versao(
        body.rotulo, body.valores, created_by=body.criado_por, notes=body.notas)
    return service_config.obter_config_versao(config_version_id)


@router.get("/config/versions/{config_version_id}/diff/{other_id}")
def diff_config_versoes(config_version_id: int, other_id: int):
    try:
        return service_config.diff_config_versoes(config_version_id, other_id)
    except ConfigVersaoInexistente as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/config/schema")
def schema_parametros():
    """Campos do `Params` Pydantic por step — fonte do formulário de configuração
    (docs/06_API_E_WEB.md §4.5)."""
    return service_config.schema_parametros()


@router.get("/config/recalibrate")
def recalibrar_threshold(t_aceita: float, t_rejeita: float, limite_amostra: int = 500):
    """Fase 9, item 6: precisão/recall de thresholds CANDIDATOS contra `label`, antes de
    o operador gravar uma `config_version` nova com eles."""
    return service_config.recalibrar_threshold(t_aceita, t_rejeita, limite_amostra=limite_amostra)


@router.get("/prompts")
def listar_prompts():
    return service_prompts.listar_prompts()


@router.get("/prompts/{name}/versions")
def versoes_prompt(name: str):
    try:
        return service_prompts.versoes_prompt(name)
    except PromptInexistente as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/prompts/{name}/versions", status_code=201)
def criar_versao_prompt(name: str, body: CriarPromptVersaoBody):
    versao_id = service_prompts.criar_versao(name, body.template, created_by=body.criado_por,
                                             notes=body.notas)
    return {"id": versao_id, "prompt_name": name}


@router.post("/prompts/{name}/versions/{version}/activate")
def ativar_versao_prompt(name: str, version: int):
    try:
        service_prompts.ativar_versao(name, version)
    except PromptInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"prompt_name": name, "versao_ativa": version}


@router.get("/prompts/{name}/diff/{version_a}/{version_b}")
def diff_versoes_prompt(name: str, version_a: int, version_b: int):
    try:
        return service_prompts.diff_versoes(name, version_a, version_b)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc
