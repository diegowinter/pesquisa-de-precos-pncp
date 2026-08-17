"""
Camada de serviço do CRUD de destinatários de notificação (Fase 9, pedido do usuário em
2026-08-17: "temos que ter um CRUD para configurar quais os destinatários que irão ser
notificados"). Mesma regra do resto do projeto: `api/` e `web/` só chamam `services/`, nunca
`db/repos` direto (docs/01_ARQUITETURA.md §7).

Regra de negócio: todo destinatário precisa de pelo menos um canal (`email` ou
`telegram_chat_id`) — a mesma exigência que a constraint `ck_notificacao_destinatario_tem_canal`
garante no banco; validar aqui também evita depender só do erro do Postgres para dar um retorno
legível na interface.
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import notificacao_destinatario as repo


class DestinatarioSemCanal(ValueError):
    """Nem `email` nem `telegram_chat_id` foram informados."""


class DestinatarioInexistente(RuntimeError):
    """`destinatario_id` não existe — 404 na API/web."""


def _validar_canal(email: str | None, telegram_chat_id: str | None) -> None:
    if not (email or "").strip() and not (telegram_chat_id or "").strip():
        raise DestinatarioSemCanal(
            "informe pelo menos um canal: e-mail ou chat_id do Telegram")


def listar_destinatarios(*, apenas_ativos: bool = False) -> list[dict[str, Any]]:
    with db.sessao() as sessao:
        return repo.listar(sessao, apenas_ativos=apenas_ativos)


def obter_destinatario(destinatario_id: int) -> dict[str, Any] | None:
    with db.sessao() as sessao:
        return repo.obter(sessao, destinatario_id)


def criar_destinatario(nome: str | None, email: str | None,
                       telegram_chat_id: str | None) -> int:
    _validar_canal(email, telegram_chat_id)
    with db.sessao() as sessao:
        return repo.criar(sessao, nome, email, telegram_chat_id)


def editar_destinatario(destinatario_id: int, nome: str | None, email: str | None,
                        telegram_chat_id: str | None) -> None:
    _validar_canal(email, telegram_chat_id)
    with db.sessao() as sessao:
        linhas = repo.editar(sessao, destinatario_id, nome, email, telegram_chat_id)
    if linhas == 0:
        raise DestinatarioInexistente(f"destinatário {destinatario_id} não existe")


def definir_ativo(destinatario_id: int, ativo: bool) -> None:
    with db.sessao() as sessao:
        linhas = repo.definir_ativo(sessao, destinatario_id, ativo)
    if linhas == 0:
        raise DestinatarioInexistente(f"destinatário {destinatario_id} não existe")


def desativar_destinatario(destinatario_id: int) -> None:
    definir_ativo(destinatario_id, False)


def ativar_destinatario(destinatario_id: int) -> None:
    definir_ativo(destinatario_id, True)
