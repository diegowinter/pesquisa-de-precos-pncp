"""
Health check por provedor (Fase 7, docs/04_FASES.md §Fase 7 item 6).

Objetivo: detectar o túnel caído (ou o servidor de GPU fora) ANTES de dar play numa etapa —
não 40 minutos depois, no meio da 6a. Uma checagem é uma sondagem HTTP leve (GET, timeout
curto) contra `base_url`; nunca uma chamada de verdade (não gasta, não é chat/embed/rerank).

`checar_capacidade` resolve a capacidade (banco → `.env`, mesma regra de `resolver.py`) e
grava o resultado em `provedor_status` quando há sessão — é o que a tela de provedores
(docs/06_API_E_WEB.md) e `runner.launcher` leem antes do play.
"""

import time
from typing import Any, TYPE_CHECKING

import requests

from pesquisa_precos.providers.resolver import (
    CapacidadeNaoConfigurada,
    resolver_capacidade,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_TIMEOUT_S = 5


# Serviços de `pncp-servicos-locais`: expõem `/health`, e não o `/models` da convenção
# OpenAI-compatible. Antes da ADR-021 estas duas podiam rodar em processo e `base_url` vazio
# significava "roda aqui" — hoje significa "não configurado", e reprovar é o certo.
_CAPACIDADES_DE_SERVICO = ("pdf", "pareamento")


def sondar_health(base_url: str, timeout_s: int = _TIMEOUT_S) -> dict[str, Any]:
    """GET em `base_url + /health` — o endpoint que os serviços `pdf` e
    `pareamento` do repo `pncp-servicos-locais` expõem. Mesma regra de `sondar_url`: responder já basta."""
    inicio = time.monotonic()
    try:
        resp = requests.get(base_url.rstrip("/") + "/health", timeout=timeout_s)
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        if resp.status_code >= 500:
            return {"saudavel": False, "latencia_ms": latencia_ms,
                    "mensagem": f"HTTP {resp.status_code} em /health"}
        return {"saudavel": True, "latencia_ms": latencia_ms, "mensagem": None}
    except requests.RequestException as exc:
        return {"saudavel": False,
                "latencia_ms": int((time.monotonic() - inicio) * 1000),
                "mensagem": f"{type(exc).__name__}: {exc}"[:300]}


def sondar_url(base_url: str, timeout_s: int = _TIMEOUT_S) -> dict[str, Any]:
    """GET simples em `base_url` (ou `base_url + /models`, que todo servidor OpenAI-compatible
    responde). Não precisa de credencial válida — só precisa RESPONDER; 401/404 ainda provam
    que o túnel está de pé, então contam como saudável. Só timeout/erro de conexão é falha."""
    if not base_url:
        return {"saudavel": False, "latencia_ms": None, "mensagem": "sem base_url configurada"}
    alvo = base_url.rstrip("/") + "/models"
    inicio = time.monotonic()
    try:
        resp = requests.get(alvo, timeout=timeout_s)
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        # Qualquer resposta HTTP (mesmo 401/404) prova que o servidor está alcançável — o que
        # se quer descartar aqui é "túnel caído", não "chave errada" (isso a etapa acusa na
        # hora, com mensagem clara — não é papel do health check adivinhar credencial).
        if resp.status_code >= 500:
            return {"saudavel": False, "latencia_ms": latencia_ms,
                    "mensagem": f"HTTP {resp.status_code} em {alvo}"}
        return {"saudavel": True, "latencia_ms": latencia_ms, "mensagem": None}
    except requests.RequestException as exc:
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        return {"saudavel": False, "latencia_ms": latencia_ms,
                "mensagem": f"{type(exc).__name__}: {exc}"[:300]}


def checar_capacidade(capacidade: str, *,
                      sessao: "Session | None" = None) -> dict[str, Any]:
    """Resolve + sonda UMA capacidade. Grava em `provedor_status` quando há `sessao` (senão só
    devolve o resultado — é o caso do `estimar()` fora de um run)."""
    # Capacidade sem provedor apontado é uma LINHA VERMELHA, não uma exceção: esta função é a
    # tela de diagnóstico e o gate pré-play, e nos dois lugares o operador precisa ver qual
    # capacidade está faltando — não uma stack trace no lugar do painel (Fase 14, ADR-022).
    try:
        resolucao = resolver_capacidade(capacidade, sessao=sessao)
    except CapacidadeNaoConfigurada as exc:
        return {"saudavel": False, "latencia_ms": None, "mensagem": str(exc),
                "capacidade": capacidade, "provedor": "—", "base_url": "",
                "origem": "não configurado"}
    if not resolucao.info.base_url:
        resultado = {"saudavel": False, "latencia_ms": None,
                     "mensagem": f"provedor `{resolucao.info.nome}` está sem base_url — "
                                 f"corrija em /providers"}
    elif capacidade in _CAPACIDADES_DE_SERVICO:
        resultado = sondar_health(resolucao.info.base_url)
    else:
        resultado = sondar_url(resolucao.info.base_url)
    resultado.update(capacidade=capacidade, provedor=resolucao.info.nome,
                     base_url=resolucao.info.base_url, origem=resolucao.origem)
    # `provedor_status` é o cache de saúde; a FK aponta para `provedor`. Desde a ADR-022 toda
    # resolução vem do banco, então sempre há linha para atualizar.
    if sessao is not None:
        from pesquisa_precos.db.repos import execution as repo
        try:
            repo.atualizar_status_provedor(sessao, resolucao.info.nome, resultado["saudavel"],
                                           resultado["latencia_ms"], resultado["mensagem"])
        except Exception as exc:  # noqa: BLE001 — ver abaixo
            # O cache é conveniência, não o resultado. Derrubar a sondagem por causa do
            # registro dela inverteria a prioridade: quem pergunta "o serviço está de pé?"
            # precisa da resposta, não do histórico.
            sessao.rollback()
            resultado["mensagem"] = (
                f"{resultado['mensagem'] or ''} "
                f"[status não registrado: {type(exc).__name__}]").strip()
    return resultado


def checar_capacidades(capacidades: list[str], *,
                       sessao: "Session | None" = None) -> list[dict[str, Any]]:
    """Uma checagem por capacidade pedida — usada por `runner.launcher` antes do play e por
    a tela `/providers` para diagnóstico manual (leitura/diagnóstico é sempre permitido,
    CLAUDE.md "Regra nº 1")."""
    return [checar_capacidade(c, sessao=sessao) for c in capacidades]


def checar_todos_ativos(sessao: "Session") -> list[dict[str, Any]]:
    """Sonda todo `provedor` marcado `ativo=true` no banco — visão geral (dashboard/CLI), não
    escopada a uma etapa. Provedores só em `.env` (banco ainda não configurado) não aparecem
    aqui; use `checar_capacidades(['chat','embed','rerank','ocr'])` para esse caso."""
    from pesquisa_precos.db.repos import execution as repo

    resultados = []
    for p in repo.listar_provedores(sessao):
        if not p.get("ativo", True):
            continue
        r = sondar_url(p["base_url"])
        r.update(provedor=p["nome"], base_url=p["base_url"])
        repo.atualizar_status_provedor(sessao, p["nome"], r["saudavel"], r["latencia_ms"],
                                       r["mensagem"])
        resultados.append(r)
    return resultados
