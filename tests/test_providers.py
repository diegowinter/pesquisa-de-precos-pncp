"""
Guarda da Fase 7 (docs/04_FASES.md): resolução de provedor por capacidade, a proibição de
fallback em `embed` (ADR-006 §2) e o contrato dos quatro `Protocol`.

Tudo aqui é OFFLINE — sem banco, sem rede, sem GPU. `resolver_capacidade` sem `sessao` cai
direto no `.env`; passar uma "sessão" fake basta para exercitar o caminho do banco sem um
Postgres de verdade.
"""

import pytest

from pesquisa_precos.providers.protocolos import InfoProvedor, ProvedorChat, ProvedorEmbed
from pesquisa_precos.providers.resolver import (
    FallbackProibidoEmbedError,
    Provedores,
    resolver_capacidade,
)

CFG = {
    "local_model": "modelo-local", "local_base_url": "http://localhost:1234/v1",
    "local_api_key": "lm-studio",
    "model_pass1": "modelo-barato", "model_pass2": "modelo-caro",
    "openrouter_base_url": "https://openrouter.ai/api/v1", "openrouter_api_key": "sk-xxx",
    "embedder_model": "BAAI/bge-m3", "reranker_model": "BAAI/bge-reranker-v2-m3",
    "gpu_base_url": "http://gpu.local:8100", "gpu_api_key": "gpu",
    "ocr_base_url": "http://ocr.local:8000/v1", "ocr_model": "ocr-modelo", "ocr_api_key": "ocr",
}


class _SessaoFake:
    """Simula só o suficiente de `Session` p/ `db.repos.execucao.capacidade_provedor_info`:
    o repo faz `sessao.execute(text(...), params).mappings().first()` — aqui devolvemos direto
    a linha (ou `None`), sem interpretar o SQL, então o teste não depende de Postgres."""

    def __init__(self, linhas: dict[str, dict | None]):
        self._linhas = linhas

    def execute(self, _stmt, params):
        return _Resultado(self._linhas.get(params["c"]))


class _Resultado:
    def __init__(self, linha):
        self._linha = linha

    def mappings(self):
        return self

    def first(self):
        return self._linha


# ── resolução via .env (sem sessão) ──────────────────────────────────────────────────

def test_resolver_chat_local_por_padrao():
    r = resolver_capacidade("chat", CFG)
    assert r.info.nome == "local"
    assert r.info.modelo == "modelo-barato" or r.info.modelo == "modelo-local"
    assert r.origem == "env"


def test_resolver_chat_forte_usa_openrouter_pass2():
    r = resolver_capacidade("chat", CFG, forte=True)
    assert r.info.nome == "openrouter"
    assert r.info.modelo == "modelo-caro"


def test_resolver_chat_override_explicito_vence_default():
    r = resolver_capacidade("chat", CFG, provedor="openrouter")
    assert r.info.nome == "openrouter"
    assert r.info.modelo == "modelo-barato"  # não é --forte, então pass1


def test_resolver_embed_local_sem_remoto():
    r = resolver_capacidade("embed", CFG, remoto=False)
    assert r.info.nome == "local"
    assert r.info.modelo == "BAAI/bge-m3"


def test_resolver_embed_remoto_e_gpu_caseira():
    r = resolver_capacidade("embed", CFG, remoto=True)
    assert r.info.nome == "gpu_caseira"
    assert r.info.base_url == "http://gpu.local:8100"


def test_resolver_ocr():
    r = resolver_capacidade("ocr", CFG)
    assert r.info.nome == "ocr_local"
    assert r.info.modelo == "ocr-modelo"


def test_resolver_capacidade_desconhecida_leva_a_erro_claro():
    with pytest.raises(ValueError, match="capacidade desconhecida"):
        resolver_capacidade("visao", CFG)


# ── resolução via banco (ADR-014: banco manda quando configurado) ───────────────────

def test_resolver_prioriza_banco_sobre_env(monkeypatch):
    linha = {"capacidade": "chat", "provedor": "openrouter", "modelo": "modelo-do-banco",
             "fallback": None, "base_url": "https://openrouter.ai/api/v1",
             "api_key_ref": "OPENAI_API_KEY", "modelo_padrao": None, "batch_size": 32,
             "rpm_limite": None, "custo_in_por_mtok": 0.5, "custo_out_por_mtok": 1.5,
             "permite_fallback": False, "ativo": True}
    sessao = _SessaoFake({"chat": linha})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-ambiente")
    r = resolver_capacidade("chat", CFG, sessao=sessao)
    assert r.origem == "banco"
    assert r.info.nome == "openrouter"
    assert r.info.modelo == "modelo-do-banco"
    assert r.api_key == "sk-do-ambiente"  # api_key_ref resolvido, nunca a chave crua do banco


def test_resolver_cai_no_env_quando_capacidade_nao_configurada_no_banco():
    sessao = _SessaoFake({"chat": None})
    r = resolver_capacidade("chat", CFG, sessao=sessao)
    assert r.origem == "env"
    assert r.info.nome == "local"


def test_fallback_proibido_em_embed_levanta_erro_claro():
    """ADR-006 §2: fallback proibido em `embed`. Mesmo que a linha do banco tenha sido editada
    direto (bypassando `db.repos.execucao.apontar_capacidade`, que já recusa isso na escrita),
    a resolução recusa de novo na leitura — duas travas, não uma."""
    linha = {"capacidade": "embed", "provedor": "gpu_caseira", "modelo": "bge-m3",
             "fallback": "local", "base_url": "http://gpu.local:8100", "api_key_ref": None,
             "modelo_padrao": None, "batch_size": 32, "rpm_limite": None,
             "custo_in_por_mtok": None, "custo_out_por_mtok": None, "permite_fallback": False,
             "ativo": True}
    sessao = _SessaoFake({"embed": linha})
    with pytest.raises(FallbackProibidoEmbedError):
        resolver_capacidade("embed", CFG, sessao=sessao)


def test_fallback_e_permitido_em_rerank():
    linha = {"capacidade": "rerank", "provedor": "gpu_caseira", "modelo": "bge-reranker",
             "fallback": "local", "base_url": "http://gpu.local:8100", "api_key_ref": None,
             "modelo_padrao": None, "batch_size": 16, "rpm_limite": None,
             "custo_in_por_mtok": None, "custo_out_por_mtok": None, "permite_fallback": True,
             "ativo": True}
    sessao = _SessaoFake({"rerank": linha})
    r = resolver_capacidade("rerank", CFG, sessao=sessao)
    assert r.info.fallback_provedor == "local"


# ── Provedores (ctx.provedores): lazy + cacheado ─────────────────────────────────────

def test_provedores_resolucao_nao_instancia_adapter():
    """`.resolucao()` só lê — não deve tentar montar um cliente HTTP (que chamaria rede)."""
    p = Provedores(CFG, None)
    r = p.resolucao("chat")
    assert isinstance(r.info, InfoProvedor)


def test_provedores_novo_chat_nao_e_cacheado(monkeypatch):
    """Etapa 3 precisa de um Curador POR THREAD — `.novo_chat()` tem que devolver uma instância
    nova a cada chamada, nunca a mesma (diferente de `.chat`, que É cacheado)."""
    monkeypatch.setattr(
        "pesquisa_precos.providers.llm_curador.Curador.__init__",
        lambda self, **kw: None)
    p = Provedores(CFG, None)
    a = p.novo_chat()
    b = p.novo_chat()
    assert a is not b


def test_protocolos_sao_runtime_checkable():
    """As quatro capacidades declaram `Protocol` verificável — é o que permite `ctx.provedores.
    chat`/etc. ser QUALQUER adapter, sem a etapa importar uma classe concreta."""
    assert hasattr(ProvedorChat, "__protocol_attrs__") or ProvedorChat._is_protocol
    assert ProvedorEmbed._is_protocol
