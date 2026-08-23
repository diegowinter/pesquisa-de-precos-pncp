"""
Autenticação da web (docs/06_API_E_WEB.md §5): sessão por cookie, password única em variável de
ambiente. Sem cadastro de usuários — "não construir gestão de usuários, papéis, SSO".

`nome_usuario` fica na sessão e alimenta `created_by`/`approved_by` nos serviços: serve para
auditoria (quem aprovou o gate), não para controle de acesso — qualquer name livre é aceito.

`WEB_SENHA` vazia/ausente desliga o login (conveniente local, igual a `API_TOKEN` na Fase 4);
quem expor a interface além do laptop precisa setar a variável.
"""

import os

from fastapi import HTTPException, Request, status


def senha_exigida() -> bool:
    return bool(os.getenv("WEB_SENHA"))


def autenticado(request: Request) -> bool:
    if not senha_exigida():
        return True
    return bool(request.session.get("autenticado"))


def exigir_login(request: Request) -> str:
    """Dependency das rotas protegidas. Devolve o name do usuário da sessão (para
    `created_by`/`approved_by`), redirecionando para `/login` via 303 quando ausente."""
    if not autenticado(request):
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return request.session.get("usuario") or "operador"


def tentar_login(request: Request, password: str, user: str) -> bool:
    esperado = os.getenv("WEB_SENHA")
    if esperado and password != esperado:
        return False
    request.session["autenticado"] = True
    request.session["usuario"] = user.strip() or "operador"
    return True


def logout(request: Request) -> None:
    request.session.clear()
