"""Corpos de requisição das rotas de comando (docs/06_API_E_WEB.md §3.2). Leitura devolve os
dicts que `services/execucao.py` já monta — não há schema de resposta redundante aqui."""

from typing import Any

from pydantic import BaseModel, Field


class CriarRunBody(BaseModel):
    label: str
    mode: str = "assisted"
    config_rotulo: str = "default"
    cost_cap_usd: float | None = None
    created_by: str | None = None


class ExecutarEtapaBody(BaseModel):
    action: str = "update"
    params_override: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class AprovarEtapaBody(BaseModel):
    approved_by: str
    params_override: dict[str, Any] = Field(default_factory=dict)


class CriarConfigVersaoBody(BaseModel):
    label: str
    valores: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    created_by: str | None = None


class CriarPromptVersaoBody(BaseModel):
    template: str
    notes: str | None = None
    created_by: str | None = None


class DestinatarioBody(BaseModel):
    name: str | None = None
    email: str | None = None
