"""
Autenticação simples (docs/06_API_E_WEB.md §5): token único em header, sem cadastro de
usuários — "não construir gestão de usuários, papéis, SSO".

`API_TOKEN` vazio/ausente desliga a checagem: conveniente para rodar local contra o Postgres
do próprio operador (ADR-001), mas quem expor a API além do laptop precisa setar a variável.
"""

import os

from fastapi import Header, HTTPException, status


async def exigir_token(x_api_token: str | None = Header(default=None, alias="X-API-Token")) -> None:
    esperado = os.getenv("API_TOKEN") or None
    if esperado is None:
        return
    if x_api_token != esperado:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "token inválido ou ausente (header X-API-Token)")
