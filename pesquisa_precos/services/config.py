"""
Camada de serviço de configuração (Fase 6, docs/04_FASES.md). Mesma regra das outras: `api/` e
`web/` só chamam `services/`, nunca `db/repos` direto (docs/01_ARQUITETURA.md §7).

Config é VERSIONADA e IMUTÁVEL (ADR-014): não há `atualizar_config`, só `criar_config_versao` —
editar sempre nasce como versão nova. `run.config_versao_id` é quem resolve "qual versão gerou
qual run" (critério de aceite da Fase 6).
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.core.regressao import Rotulo, avaliar
from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo
from pesquisa_precos.db.repos import par as repo_par


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
    "formulário por etapa, gerado do Pydantic"). Desde a Fase 13 é a ÚNICA superfície de
    configuração — mudar um `Params` chega aqui sozinho, sem formulário à parte para lembrar."""
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


def recalibrar_threshold(t_aceita: float, t_rejeita: float, *,
                         limite_amostra: int = 500) -> dict[str, Any]:
    """Fase 9, item 6: precisão/recall de um par de thresholds CANDIDATOS, usando `rotulo`,
    ANTES de gravar uma `config_versao` nova (a interface só grava depois de o operador olhar
    este número — nada aqui persiste config). Mesma lógica de `core.regressao`, usada também
    pela suite de regressão em `ferramentas/regressao.py`: uma verdade só sobre o que os
    thresholds decidem."""
    with db.sessao() as sessao:
        linhas = repo_par.amostra_rotulos(sessao, limite_amostra)
    rotulos = [Rotulo(par_key=l["par_key"], score_rerank=l["score_rerank"],
                      decisao_final=l["decisao_final"]) for l in linhas]
    resultado = avaliar(rotulos, t_aceita=t_aceita, t_rejeita=t_rejeita)
    return {
        "t_aceita": t_aceita, "t_rejeita": t_rejeita, "n_amostra": resultado.n_amostra,
        "n_decididos": resultado.n_decididos, "n_ambiguos": resultado.n_ambiguos,
        "precisao": resultado.precisao, "recall": resultado.recall,
        "verdadeiros_positivos": resultado.verdadeiros_positivos,
        "falsos_positivos": resultado.falsos_positivos,
        "verdadeiros_negativos": resultado.verdadeiros_negativos,
        "falsos_negativos": resultado.falsos_negativos,
    }
