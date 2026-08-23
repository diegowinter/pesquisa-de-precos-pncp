"""
Guarda da Fase 7 + Fase 14: resolução de provider por capability, a proibição de fallback em
`embed` (ADR-006 §2) e o contrato dos `Protocol`.

Tudo aqui é OFFLINE — sem banco, sem rede, sem GPU: uma "sessão" fake basta para exercitar a
resolução sem um Postgres de verdade.

Antes da ADR-022 metade deste arquivo testava a resolução pelo `.env`. Esse caminho **saiu**, e
os testes que o cobriam viraram o inverso: sem provider apontado, resolver LEVANTA. É a mesma
troca que a ADR-020 fez com `--fonte csv` — o que era fallback virou erro.
"""

import pytest

from pesquisa_precos.providers.protocolos import ProviderInfo, ChatProvider, EmbedProvider
from pesquisa_precos.providers.resolver import (
    CapabilityNotConfigured,
    FallbackProibidoEmbedError,
    Providers,
    resolver_capacidade,
)


def _linha(capability: str, **extra) -> dict:
    """Uma linha de `provider_capability` JOIN `provider`, como o repo a devolve."""
    base = {"capability": capability, "provider": "openrouter", "model": None,
            "fallback": None, "base_url": "https://openrouter.ai/api/v1",
            "api_key_encrypted": None, "default_model": None, "batch_size": 32,
            "rpm_limit": None, "cost_in_per_mtok": None, "cost_out_per_mtok": None,
            "cost_usd_per_call": None, "allows_fallback": False, "active": True}
    return {**base, **extra}






class _SessaoFake:
    """Simula só o suficiente de `Session` p/ `db.repos.execution.capacidade_provedor_info`:
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


# ── sem provider apontado, resolver LEVANTA (ADR-022) ───────────────────────────────

@pytest.mark.parametrize("capability", ["chat", "embed", "rerank", "pdf", "matching"])
def test_capacidade_sem_provedor_levanta(capability):
    """O caminho `.env` saiu. Não existe mais "cai para o default" — que era exatamente o mode
    em que um erro de configuração virava a step rodando com o model errado."""
    sessao = _SessaoFake({capability: None})
    with pytest.raises(CapabilityNotConfigured) as exc:
        resolver_capacidade(capability, sessao=sessao)
    assert "/providers" in str(exc.value)


def test_mensagem_diz_o_que_fazer():
    """Erro de configuração tem de apontar a saída: a tela, não o arquivo que saiu de cena."""
    sessao = _SessaoFake({"chat": None})
    with pytest.raises(CapabilityNotConfigured) as exc:
        resolver_capacidade("chat", sessao=sessao)
    assert "chat" in str(exc.value) and "/providers" in str(exc.value)


def test_ocr_nao_e_mais_capacidade_deste_processo():
    """Quem chama o OCR é o serviço de `pdf`, na máquina dele (ADR-021). Resolver `ocr` aqui
    significaria que alguém voltou a rasterizar página neste processo."""
    with pytest.raises(ValueError, match="capability desconhecida"):
        resolver_capacidade("ocr", sessao=_SessaoFake({}))


def test_resolver_capacidade_desconhecida_leva_a_erro_claro():
    with pytest.raises(ValueError, match="capability desconhecida"):
        resolver_capacidade("vision", sessao=_SessaoFake({}))


# ── resolução via banco (ADR-014: banco manda quando configurado) ───────────────────

def test_resolver_le_o_provedor_do_banco():
    sessao = _SessaoFake({"chat": _linha("chat", model="model-do-banco",
                                         cost_in_per_mtok=0.5, cost_out_per_mtok=1.5,
                                         cost_usd_per_call=0.0001)})
    r = resolver_capacidade("chat", sessao=sessao)
    assert r.source == "banco"
    assert r.info.name == "openrouter"
    assert r.info.model == "model-do-banco"
    assert r.info.cost_usd_per_call == 0.0001


def test_modelo_da_capacidade_vence_o_padrao_do_provedor():
    """`provider_capability.model` é o override por capability; `provider.default_model` é o
    default. Um provider pode atender `chat` com modelos diferentes em instalações diferentes."""
    sessao = _SessaoFake({"chat": _linha("chat", model="especifico", default_model="padrao")})
    assert resolver_capacidade("chat", sessao=sessao).info.model == "especifico"
    sessao = _SessaoFake({"chat": _linha("chat", model=None, default_model="padrao")})
    assert resolver_capacidade("chat", sessao=sessao).info.model == "padrao"


def test_chave_cifrada_e_decifrada_na_resolucao(monkeypatch):
    """O resolver é o único ponto que devolve segredo em claro — e tem de devolvê-lo certo."""
    from pesquisa_precos.db import secret as seg

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    blob = seg.cifrar("sk-or-v1-do-banco", context="openrouter")
    sessao = _SessaoFake({"chat": _linha("chat", api_key_encrypted=blob)})
    assert resolver_capacidade("chat", sessao=sessao).api_key == "sk-or-v1-do-banco"


def test_provedor_sem_chave_resolve_com_string_vazia():
    """Serviço local sem autenticação é caso legítimo — não pode explodir por falta de key."""
    sessao = _SessaoFake({"matching": _linha("matching", base_url="http://x:8300")})
    assert resolver_capacidade("matching", sessao=sessao).api_key == ""


def test_fallback_proibido_em_embed_levanta_erro_claro():
    """ADR-006 §2: fallback proibido em `embed`. Mesmo que a linha do banco tenha sido editada
    direto (bypassando `db.repos.execution.apontar_capacidade`, que já recusa isso na escrita),
    a resolução recusa de novo na leitura — duas travas, não uma."""
    sessao = _SessaoFake({"embed": _linha("embed", provider="gpu_caseira", model="bge-m3",
                                          fallback="local",
                                          base_url="http://gpu.local:8100")})
    with pytest.raises(FallbackProibidoEmbedError):
        resolver_capacidade("embed", sessao=sessao)


def test_fallback_e_permitido_em_rerank():
    sessao = _SessaoFake({"rerank": _linha(
        "rerank", provider="gpu_caseira", model="bge-reranker", fallback="local",
        base_url="http://gpu.local:8100", batch_size=16, allows_fallback=True)})
    r = resolver_capacidade("rerank", sessao=sessao)
    assert r.info.fallback_provider == "local"


# ── Providers (ctx.providers): lazy + cacheado ─────────────────────────────────────

def test_provedores_resolucao_nao_instancia_adapter():
    """`.resolucao()` só lê — não deve tentar montar um cliente HTTP (que chamaria rede)."""
    p = Providers(_SessaoFake({"chat": _linha("chat")}))
    r = p.resolucao("chat")
    assert isinstance(r.info, ProviderInfo)


def test_provedores_novo_chat_nao_e_cacheado(monkeypatch):
    """Etapa 3 precisa de um Curador POR THREAD — `.novo_chat()` tem que devolver uma instância
    nova a cada chamada, nunca a mesma (diferente de `.chat`, que É cacheado)."""
    monkeypatch.setattr(
        "pesquisa_precos.providers.llm_curador.Curador.__init__",
        lambda self, **kw: None)
    p = Providers(_SessaoFake({"chat": _linha("chat")}))
    a = p.novo_chat()
    b = p.novo_chat()
    assert a is not b


def test_protocolos_sao_runtime_checkable():
    """As quatro capabilities declaram `Protocol` verificável — é o que permite `ctx.providers.
    chat`/etc. ser QUALQUER adapter, sem a step importar uma classe concreta."""
    assert hasattr(ChatProvider, "__protocol_attrs__") or ChatProvider._is_protocol
    assert EmbedProvider._is_protocol
