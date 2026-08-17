"""
Repositório de `notificacao_destinatario` — CRUD dos destinatários de notificação (Fase 9,
canal Resend/e-mail). A credencial (API key do Resend) não mora aqui — só em `.env`
(ADR-006); esta tabela guarda apenas QUEM recebe.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def listar(sessao: Session, *, apenas_ativos: bool = False) -> list[dict[str, Any]]:
    filtro = "WHERE ativo" if apenas_ativos else ""
    linhas = sessao.execute(text(
        f"SELECT id, nome, email, ativo, criado_em "
        f"FROM notificacao_destinatario {filtro} ORDER BY id")).mappings().all()
    return [dict(r) for r in linhas]


def obter(sessao: Session, destinatario_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(text(
        "SELECT id, nome, email, ativo, criado_em "
        "FROM notificacao_destinatario WHERE id = :id"),
        {"id": destinatario_id}).mappings().first()
    return dict(linha) if linha else None


def criar(sessao: Session, nome: str | None, email: str) -> int:
    return sessao.execute(
        text("INSERT INTO notificacao_destinatario (nome, email) "
             "VALUES (:n, :e) RETURNING id"),
        {"n": nome or None, "e": email},
    ).scalar_one()


def editar(sessao: Session, destinatario_id: int, nome: str | None, email: str) -> int:
    """Devolve o número de linhas afetadas (0 = id inexistente)."""
    return sessao.execute(
        text("UPDATE notificacao_destinatario "
             "SET nome = :n, email = :e "
             "WHERE id = :id"),
        {"id": destinatario_id, "n": nome or None, "e": email},
    ).rowcount


def definir_ativo(sessao: Session, destinatario_id: int, ativo: bool) -> int:
    return sessao.execute(
        text("UPDATE notificacao_destinatario SET ativo = :a WHERE id = :id"),
        {"id": destinatario_id, "a": ativo},
    ).rowcount
