"""
Rotas `/api/notifications/recipients/*` — CRUD dos destinatários de notificação (Fase 9,
pedido do usuário em 2026-08-17). Toda rota chama `services/`, nunca `db/repos` direto
(docs/01_ARQUITETURA.md §7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pesquisa_precos.api.schemas import DestinatarioBody
from pesquisa_precos.services import notification_recipients as service
from pesquisa_precos.services.notification_recipients import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)

router = APIRouter(tags=["notificacoes"])


@router.get("/notifications/recipients")
def listar_destinatarios(apenas_ativos: bool = False):
    return service.listar_destinatarios(apenas_ativos=apenas_ativos)


@router.get("/notifications/recipients/{recipient_id}")
def obter_destinatario(recipient_id: int):
    destinatario = service.obter_destinatario(recipient_id)
    if destinatario is None:
        raise HTTPException(404, f"destinatário {recipient_id} não existe")
    return destinatario


@router.post("/notifications/recipients", status_code=201)
def criar_destinatario(body: DestinatarioBody):
    try:
        recipient_id = service.criar_destinatario(body.name, body.email)
    except DestinatarioSemCanal as exc:
        raise HTTPException(422, str(exc)) from exc
    return service.obter_destinatario(recipient_id)


@router.put("/notifications/recipients/{recipient_id}")
def editar_destinatario(recipient_id: int, body: DestinatarioBody):
    try:
        service.editar_destinatario(recipient_id, body.name, body.email)
    except DestinatarioSemCanal as exc:
        raise HTTPException(422, str(exc)) from exc
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return service.obter_destinatario(recipient_id)


@router.post("/notifications/recipients/{recipient_id}/deactivate")
def desativar_destinatario(recipient_id: int):
    try:
        service.desativar_destinatario(recipient_id)
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return service.obter_destinatario(recipient_id)


@router.post("/notifications/recipients/{recipient_id}/activate")
def ativar_destinatario(recipient_id: int):
    try:
        service.ativar_destinatario(recipient_id)
    except DestinatarioInexistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return service.obter_destinatario(recipient_id)
