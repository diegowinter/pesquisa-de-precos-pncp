"""
Rotas `/api/notificacoes/destinatarios/*` — CRUD dos destinatários de notificação (Fase 9,
pedido do usuário em 2026-08-17). Toda rota chama `services/`, nunca `db/repos` direto
(docs/01_ARQUITETURA.md §7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pesquisa_precos.api.schemas import DestinatarioBody
from pesquisa_precos.services import notificacao_destinatarios as servico
from pesquisa_precos.services.notificacao_destinatarios import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)

router = APIRouter(tags=["notificacoes"])


@router.get("/notificacoes/destinatarios")
def listar_destinatarios(apenas_ativos: bool = False):
    return servico.listar_destinatarios(apenas_ativos=apenas_ativos)


@router.get("/notificacoes/destinatarios/{destinatario_id}")
def obter_destinatario(destinatario_id: int):
    destinatario = servico.obter_destinatario(destinatario_id)
    if destinatario is None:
        raise HTTPException(404, f"destinatário {destinatario_id} não existe")
    return destinatario


@router.post("/notificacoes/destinatarios", status_code=201)
def criar_destinatario(body: DestinatarioBody):
    try:
        destinatario_id = servico.criar_destinatario(body.nome, body.email, body.telegram_chat_id)
    except DestinatarioSemCanal as exc:
        raise HTTPException(422, str(exc)) from exc
    return servico.obter_destinatario(destinatario_id)


@router.put("/notificacoes/destinatarios/{destinatario_id}")
def editar_destinatario(destinatario_id: int, body: DestinatarioBody):
    try:
        servico.editar_destinatario(destinatario_id, body.nome, body.email, body.telegram_chat_id)
    except DestinatarioSemCanal as exc:
        raise HTTPException(422, str(exc)) from exc
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return servico.obter_destinatario(destinatario_id)


@router.post("/notificacoes/destinatarios/{destinatario_id}/desativar")
def desativar_destinatario(destinatario_id: int):
    try:
        servico.desativar_destinatario(destinatario_id)
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return servico.obter_destinatario(destinatario_id)


@router.post("/notificacoes/destinatarios/{destinatario_id}/ativar")
def ativar_destinatario(destinatario_id: int):
    try:
        servico.ativar_destinatario(destinatario_id)
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return servico.obter_destinatario(destinatario_id)
