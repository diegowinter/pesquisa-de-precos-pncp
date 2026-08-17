"""
Camada de serviço de prompts (Fase 6). `prompt`/`prompt_versao` — versão ativa + histórico
(docs/02_SCHEMA.md §10). Editar nunca sobrescreve: `criar_prompt_versao` sempre nasce inativa;
`ativar_prompt_versao` é quem promove.

A resolução em tempo de execução (o que `providers/llm_curador.py` de fato usa para montar o
prompt de uma chamada) mora em `core/prompts_resolver.py`, não aqui — este módulo é só CRUD
para a tela de edição e para a API.
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo


class PromptInexistente(RuntimeError):
    """Nome de prompt ou versão que não existe — 404 na API."""


def listar_prompts() -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.listar_prompts(sessao)


def versoes_prompt(nome: str) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        versoes = repo.prompt_versoes(sessao, nome)
    if not versoes:
        raise PromptInexistente(f"prompt {nome!r} não existe")
    return versoes


def criar_versao(nome: str, template: str, *, criado_por: str | None = None,
                 notas: str | None = None) -> int:
    with db.sessao() as sessao:
        return repo.criar_prompt_versao(sessao, nome, template, criado_por=criado_por,
                                        notas=notas)


def ativar_versao(nome: str, versao: int) -> bool:
    with db.sessao() as sessao:
        ok = repo.ativar_prompt_versao(sessao, nome, versao)
    if not ok:
        raise PromptInexistente(f"prompt {nome!r} versão {versao} não existe")
    return ok


def diff_versoes(nome: str, versao_a: int, versao_b: int) -> dict[str, Any]:
    with db.sessao() as sessao:
        return repo.diff_prompt(sessao, nome, versao_a, versao_b)
