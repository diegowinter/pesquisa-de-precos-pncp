"""
Camada de serviço do CRUD de destinatários de notificação (Fase 9, pedido do usuário em
2026-08-17: "temos que ter um CRUD para configurar quais os destinatários que irão ser
notificados"). Mesma regra do resto do projeto: `api/` e `web/` só chamam `services/`, nunca
`db/repos` direto (docs/01_ARQUITETURA.md §7).

Canal único: e-mail via Resend (Telegram foi removido, pedido do usuário em 2026-08-17 — "menos
complexidade"). `email` é obrigatório para todo destinatário.
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import notification_recipient as repo


class DestinatarioSemCanal(ValueError):
    """`email` não foi informado."""


class DestinatarioInexistente(RuntimeError):
    """`destinatario_id` não existe — 404 na API/web."""


def _validar_canal(email: str | None) -> None:
    if not (email or "").strip():
        raise DestinatarioSemCanal("informe o e-mail do destinatário")


def listar_destinatarios(*, apenas_ativos: bool = False) -> list[dict[str, Any]]:
    with db.session() as sessao:
        return repo.listar(sessao, apenas_ativos=apenas_ativos)


def obter_destinatario(destinatario_id: int) -> dict[str, Any] | None:
    with db.session() as sessao:
        return repo.obter(sessao, destinatario_id)


def criar_destinatario(name: str | None, email: str | None) -> int:
    _validar_canal(email)
    with db.session() as sessao:
        return repo.criar(sessao, name, email)  # type: ignore[arg-type]


def editar_destinatario(destinatario_id: int, name: str | None, email: str | None) -> None:
    _validar_canal(email)
    with db.session() as sessao:
        linhas = repo.editar(sessao, destinatario_id, name, email)  # type: ignore[arg-type]
    if linhas == 0:
        raise DestinatarioInexistente(f"destinatário {destinatario_id} não existe")


def definir_ativo(destinatario_id: int, active: bool) -> None:
    with db.session() as sessao:
        linhas = repo.definir_ativo(sessao, destinatario_id, active)
    if linhas == 0:
        raise DestinatarioInexistente(f"destinatário {destinatario_id} não existe")


def desativar_destinatario(destinatario_id: int) -> None:
    definir_ativo(destinatario_id, False)


def ativar_destinatario(destinatario_id: int) -> None:
    definir_ativo(destinatario_id, True)
