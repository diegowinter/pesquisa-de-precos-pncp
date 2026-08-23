"""
Seed de provedores a partir do `.env` (Fase 14 bloco 4, ADR-022).

`plano()` é puro — lê o ambiente e devolve o que faria. Todos os testes daqui exercitam só ele,
sem banco e sem escrita: é o `--conferir` do script.

O teste que mais importa é `test_modelo_caro_nao_e_semeado`: semear o PASS2 criaria um provedor
pronto para ser apontado por engano, contra a restrição de custo do CLAUDE.md.
"""

import pytest

from tools.seed_providers import plano


@pytest.fixture(autouse=True)
def _ambiente_limpo(monkeypatch):
    for var in ("OPENAI_BASE_URL", "OPENAI_MODEL_PASS1", "OPENAI_MODEL_PASS2", "OPENAI_API_KEY",
                "LOCAL_BASE_URL", "LOCAL_MODEL", "LOCAL_API_KEY", "GPU_BASE_URL", "GPU_API_KEY",
                "EMBEDDER_MODEL", "RERANKER_MODEL", "PDF_BASE_URL", "PDF_API_KEY",
                "PAREAMENTO_BASE_URL", "PAREAMENTO_API_KEY", "CUSTO_USD_CHAMADA_PASS1"):
        monkeypatch.delenv(var, raising=False)


def _por_nome(provedores):
    return {p["nome"]: p for p in provedores}


def test_openrouter_vira_provedor_de_chat(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_MODEL_PASS1", "modelo-barato")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-x")
    monkeypatch.setenv("CUSTO_USD_CHAMADA_PASS1", "0.0001")

    provedores, apontamentos, _ = plano()
    p = _por_nome(provedores)["openrouter"]
    assert p["capacidades"] == ["chat"]
    assert p["modelo_padrao"] == "modelo-barato"
    assert p["custo_usd_chamada"] == 0.0001
    assert p["api_key"] == "sk-or-v1-x"
    assert {"capacidade": "chat", "provedor": "openrouter"} in apontamentos


def test_modelo_caro_nao_e_semeado(monkeypatch):
    """ADR-004 + restrição de custo do CLAUDE.md: o PASS2 não entra, e o script AVISA que
    deixou de entrar — silêncio aqui viraria "sumiu, será que era para ter vindo?"."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_MODEL_PASS1", "modelo-barato")
    monkeypatch.setenv("OPENAI_MODEL_PASS2", "modelo-caro")

    provedores, _, avisos = plano()
    modelos = [p.get("modelo_padrao") for p in provedores]
    assert "modelo-caro" not in modelos
    assert any("PASS2" in a for a in avisos)


def test_lm_studio_entra_mas_nao_e_apontado(monkeypatch):
    """Apontar dois provedores para `chat` em sequência faria o último vencer em silêncio —
    quem atende `chat` é decisão do operador, na tela."""
    monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LOCAL_MODEL", "gemma")

    provedores, apontamentos, _ = plano()
    assert "lm_studio" in _por_nome(provedores)
    assert not [a for a in apontamentos if a["provedor"] == "lm_studio"]


def test_lm_studio_e_gratis_nao_desconhecido(monkeypatch):
    """`0.0` e `None` significam coisas diferentes: "grátis, e eu sei" vs. "não informado"."""
    monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LOCAL_MODEL", "gemma")

    provedores, _, _ = plano()
    assert _por_nome(provedores)["lm_studio"]["custo_usd_chamada"] == 0.0


def test_gpu_atende_embed_e_rerank_com_modelos_diferentes(monkeypatch):
    monkeypatch.setenv("GPU_BASE_URL", "https://tunel.ngrok.dev")
    monkeypatch.setenv("EMBEDDER_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

    provedores, apontamentos, _ = plano()
    assert set(_por_nome(provedores)["gpu_caseira"]["capacidades"]) == {"embed", "rerank"}
    por_capacidade = {a["capacidade"]: a for a in apontamentos}
    assert por_capacidade["embed"]["modelo"] == "BAAI/bge-m3"
    assert por_capacidade["rerank"]["modelo"] == "BAAI/bge-reranker-v2-m3"


def test_servicos_do_companion(monkeypatch):
    monkeypatch.setenv("PDF_BASE_URL", "http://pdf:8200")
    monkeypatch.setenv("PAREAMENTO_BASE_URL", "http://par:8300")

    provedores, apontamentos, avisos = plano()
    nomes = _por_nome(provedores)
    assert nomes["service_pdf"]["base_url"] == "http://pdf:8200"
    assert nomes["service_pareamento"]["base_url"] == "http://par:8300"
    assert {a["capacidade"] for a in apontamentos} >= {"pdf", "pareamento"}
    assert not [a for a in avisos if "BASE_URL" in a]


def test_service_sem_url_vira_aviso_nao_provedor_quebrado():
    """Semear um provedor com `base_url` vazia criaria exatamente a linha que a ADR-021 proíbe.
    Melhor não cadastrar e dizer o que falta."""
    provedores, _, avisos = plano()
    assert not [p for p in provedores if not p["base_url"]]
    assert len([a for a in avisos if "BASE_URL" in a]) == 2   # pdf e pareamento


def test_env_vazio_nao_semeia_nada():
    provedores, apontamentos, _ = plano()
    assert provedores == [] and apontamentos == []


def test_plano_nao_escreve_nada(monkeypatch):
    """`plano()` é puro — o `--conferir` do script depende disso."""
    monkeypatch.setenv("GPU_BASE_URL", "https://tunel.ngrok.dev")

    def explodir(*_a, **_k):
        raise AssertionError("plano() não pode abrir sessão de banco")

    monkeypatch.setattr("pesquisa_precos.db.session.create_session", explodir)
    plano()
