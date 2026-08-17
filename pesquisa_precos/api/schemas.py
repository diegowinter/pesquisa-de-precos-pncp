"""Corpos de requisição das rotas de comando (docs/06_API_E_WEB.md §3.2). Leitura devolve os
dicts que `services/execucao.py` já monta — não há schema de resposta redundante aqui."""

from typing import Any

from pydantic import BaseModel, Field


class CriarRunBody(BaseModel):
    rotulo: str
    modo: str = "assistido"
    config_rotulo: str = "default"
    teto_custo_usd: float | None = None
    criado_por: str | None = None


class ExecutarEtapaBody(BaseModel):
    acao: str = "atualizar"
    params_override: dict[str, Any] = Field(default_factory=dict)
    confirmar: bool = False


class AprovarEtapaBody(BaseModel):
    aprovado_por: str
    params_override: dict[str, Any] = Field(default_factory=dict)


class CriarConfigVersaoBody(BaseModel):
    rotulo: str
    valores: dict[str, Any] = Field(default_factory=dict)
    notas: str | None = None
    criado_por: str | None = None


class CriarPromptVersaoBody(BaseModel):
    template: str
    notas: str | None = None
    criado_por: str | None = None


class DestinatarioBody(BaseModel):
    nome: str | None = None
    email: str | None = None
