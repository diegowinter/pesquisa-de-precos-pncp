"""
Camada de serviço de configuração (Fase 6, docs/04_FASES.md). Mesma regra das outras: `api/` e
`web/` só chamam `services/`, nunca `db/repos` direto (docs/01_ARQUITETURA.md §7).

Config é VERSIONADA e IMUTÁVEL (ADR-014): não há `atualizar_config`, só `criar_config_versao` —
editar sempre nasce como versão nova. `run.config_versao_id` é quem resolve "qual versão gerou
qual run" (critério de aceite da Fase 6).
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo


class ConfigVersaoInexistente(RuntimeError):
    """`config_versao_id` não existe — 404 na API."""


def listar_config_versoes() -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.listar_config_versoes(sessao)


def obter_config_versao(config_versao_id: int) -> dict[str, Any] | None:
    with db.sessao() as sessao:
        return repo.config_versao_por_id(sessao, config_versao_id)


def criar_config_versao(rotulo: str, valores: dict[str, Any], *,
                        criado_por: str | None = None, notas: str | None = None) -> int:
    with db.sessao() as sessao:
        config_versao_id = repo.criar_config_versao(sessao, rotulo, criado_por=criado_por,
                                                     notas=notas)
        repo.gravar_config(sessao, config_versao_id, valores)
    return config_versao_id


def diff_config_versoes(id_a: int, id_b: int) -> dict[str, Any]:
    with db.sessao() as sessao:
        if repo.config_versao_por_id(sessao, id_a) is None:
            raise ConfigVersaoInexistente(f"config_versao {id_a} não existe")
        if repo.config_versao_por_id(sessao, id_b) is None:
            raise ConfigVersaoInexistente(f"config_versao {id_b} não existe")
        return repo.diff_config(sessao, id_a, id_b)


def schema_parametros() -> dict[str, Any]:
    """Um bloco por etapa com os campos do `Params` Pydantic (nome, tipo, default, descrição) —
    é o que a tela de configuração usa para gerar o formulário (docs/06_API_E_WEB.md §4.5:
    "formulário por etapa, gerado do Pydantic"). Mesma fonte que `cli/flags.py` usa para as
    flags — mudar um `Params` nunca exige lembrar de atualizar um formulário à parte."""
    from pesquisa_precos.etapas import registry

    saida: dict[str, Any] = {}
    for definicao in registry.ordem():
        campos = {}
        for nome, campo in definicao.params_model.model_fields.items():
            campos[nome] = {
                "tipo": str(campo.annotation),
                "default": campo.get_default(call_default_factory=True),
                "descricao": campo.description or "",
            }
        saida[definicao.chave] = {"titulo": definicao.titulo, "campos": campos}
    return saida
