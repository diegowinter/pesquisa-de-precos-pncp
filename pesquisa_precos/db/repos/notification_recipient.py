"""
Repositório de `notification_recipient` — CRUD dos destinatários de notificação (Fase 9,
canal Resend/e-mail). A credencial (API chave do Resend) não mora aqui — só em `.env`
(ADR-006); esta tabela guarda apenas QUEM recebe.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def listar(sessao: Session, *, apenas_ativos: bool = False) -> list[dict[str, Any]]:
    filtro = "WHERE active" if apenas_ativos else ""
    linhas = sessao.execute(text(
        f"SELECT id, name, email, active, created_at "
        f"FROM notification_recipient {filtro} ORDER BY id")).mappings().all()
    return [dict(r) for r in linhas]


def obter(sessao: Session, destinatario_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(text(
        "SELECT id, name, email, active, created_at "
        "FROM notification_recipient WHERE id = :id"),
        {"id": destinatario_id}).mappings().first()
    return dict(linha) if linha else None


def criar(sessao: Session, name: str | None, email: str) -> int:
    return sessao.execute(
        text("INSERT INTO notification_recipient (name, email) "
             "VALUES (:n, :e) RETURNING id"),
        {"n": name or None, "e": email},
    ).scalar_one()


def editar(sessao: Session, destinatario_id: int, name: str | None, email: str) -> int:
    """Devolve o número de linhas afetadas (0 = id inexistente)."""
    return sessao.execute(
        text("UPDATE notification_recipient "
             "SET name = :n, email = :e "
             "WHERE id = :id"),
        {"id": destinatario_id, "n": name or None, "e": email},
    ).rowcount


def definir_ativo(sessao: Session, destinatario_id: int, active: bool) -> int:
    return sessao.execute(
        text("UPDATE notification_recipient SET active = :a WHERE id = :id"),
        {"id": destinatario_id, "a": active},
    ).rowcount
