"""
Rotas HTML de `/providers` (Fase 14 bloco 2, ADR-022).

Subir a app num `TestClient` não é executar step (CLAUDE.md, regra nº 1): nenhuma rota aqui
dispara `POST .../run` nem `.../approve`. O `testar` de provider é sondagem HTTP e está
mockado — nunca bate na rede de verdade, mesmo padrão de `tests/test_notifications.py`.

O teste que mais importa é `test_chave_nunca_aparece_no_html`: se ele cair, a razão que
permitiu a key sair do `.env` deixou de valer.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from pesquisa_precos.db import secret as seg
from pesquisa_precos.db import session as db
from pesquisa_precos.services import providers as service

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

PREFIXO = "teste-f14w-"
CHAVE = "sk-or-v1-nao-pode-vazar"


def _limpar():
    with db.session() as sessao:
        sessao.execute(text("DELETE FROM provider_capability WHERE provider LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provider_status WHERE provider LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provider WHERE name LIKE :p"), {"p": f"{PREFIXO}%"})


def _snapshot_capabilities():
    """Fotografa `provider_capability` inteira.

    Os testes apontam capabilities REAIS (`chat`, `embed`, ...) para provedores fictícios — não
    há capability "de teste", o enum é fechado. Sem restaurar depois, rodar `pytest` apagaria a
    configuração de produção do operador: as etapas parariam de resolver provider e a culpa
    pareceria do código. Já aconteceu uma vez.
    """
    with db.session() as sessao:
        return [dict(r) for r in sessao.execute(text(
            "SELECT capability, provider, model, fallback FROM provider_capability"
        )).mappings()]


def _restaurar_capabilities(linhas):
    from pesquisa_precos.db.repos import execution as repo

    with db.session() as sessao:
        sessao.execute(text("DELETE FROM provider_capability"))
        for linha in linhas:
            repo.apontar_capacidade(sessao, linha["capability"], linha["provider"],
                                    linha["model"], linha["fallback"])


@pytest.fixture
def cliente(monkeypatch):
    from fastapi.testclient import TestClient

    from pesquisa_precos.web.app import app

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.delenv("WEB_SENHA", raising=False)   # login desligado, como local
    capabilities = _snapshot_capabilities()
    _limpar()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    _limpar()
    _restaurar_capabilities(capabilities)


def test_criar_provedor_pela_tela(cliente):
    resp = cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "https://openrouter.ai/api/v1",
        "capabilities": ["chat"], "default_model": "model-barato",
        "batch_size": "16", "cost_in_per_mtok": "0.01", "active": "on", "api_key": CHAVE})
    assert resp.status_code == 303
    p = service.obter(f"{PREFIXO}or")
    assert p["base_url"] == "https://openrouter.ai/api/v1"
    assert p["batch_size"] == 16 and p["has_api_key"] is True


def test_campos_numericos_vazios_nao_quebram(cliente):
    """Campo numérico de formulário HTML chega como `''`, não `None`."""
    resp = cliente.post("/providers", data={
        "name": f"{PREFIXO}gpu", "base_url": "http://gpu:8100", "capabilities": ["embed"],
        "batch_size": "", "rpm_limit": "", "cost_in_per_mtok": "", "active": "on"})
    assert resp.status_code == 303
    p = service.obter(f"{PREFIXO}gpu")
    assert p["rpm_limit"] is None and p["cost_in_per_mtok"] is None


def test_chave_nunca_aparece_no_html(cliente):
    """A promessa da ADR-022: a key entra e não volta. Nem na listagem, nem no formulário
    de edição, nem num `value=` de input."""
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"],
        "active": "on", "api_key": CHAVE})

    html = cliente.get("/providers").text
    assert CHAVE not in html and "nao-pode-vazar" not in html
    assert "azar" in html            # os 4 últimos dígitos, esses sim

    html_edicao = cliente.get(f"/providers?editar={PREFIXO}or").text
    assert CHAVE not in html_edicao
    assert 'name="api_key"' in html_edicao          # o campo existe...
    assert f'value="{CHAVE}"' not in html_edicao    # ...e nasce vazio


def test_editar_sem_preencher_a_chave_mantem_a_gravada(cliente):
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"],
        "active": "on", "api_key": CHAVE})
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://novo", "capabilities": ["chat"],
        "active": "on", "api_key": ""})
    p = service.obter(f"{PREFIXO}or")
    assert p["base_url"] == "http://novo" and p["has_api_key"] is True


def test_limpar_chave_pela_tela(cliente):
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"],
        "active": "on", "api_key": CHAVE})
    assert cliente.post(f"/providers/{PREFIXO}or/key/clear").status_code == 303
    assert service.obter(f"{PREFIXO}or")["has_api_key"] is False


def test_formulario_invalido_volta_com_erro(cliente):
    resp = cliente.post("/providers", data={
        "name": f"{PREFIXO}x", "base_url": "", "capabilities": ["chat"], "active": "on"})
    assert resp.status_code == 303
    assert "erro=" in resp.headers["location"]
    assert service.obter(f"{PREFIXO}x") is None


def test_apontar_capacidade_pela_tela(cliente):
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"], "active": "on"})
    resp = cliente.post("/providers/capabilities",
                        data={"capability": "chat", "provider": f"{PREFIXO}or",
                              "model": "model-x", "fallback": ""})
    assert resp.status_code == 303 and "erro=" not in resp.headers["location"]


def test_fallback_em_embed_recusado_pela_tela(cliente):
    for sufixo in ("a", "b"):
        cliente.post("/providers", data={
            "name": f"{PREFIXO}{sufixo}", "base_url": f"http://{sufixo}",
            "capabilities": ["embed"], "active": "on"})
    resp = cliente.post("/providers/capabilities",
                        data={"capability": "embed", "provider": f"{PREFIXO}a",
                              "fallback": f"{PREFIXO}b"})
    assert resp.status_code == 303 and "erro=" in resp.headers["location"]


def test_ativar_desativar_pela_tela(cliente):
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"], "active": "on"})
    cliente.post(f"/providers/{PREFIXO}or/active", data={"active": "off"})
    assert service.obter(f"{PREFIXO}or")["active"] is False
    cliente.post(f"/providers/{PREFIXO}or/active", data={"active": "on"})
    assert service.obter(f"{PREFIXO}or")["active"] is True


def test_testar_provedor_grava_status(cliente):
    """Sondagem mockada: o teste é sobre gravar `provider_status`, não sobre a rede."""
    cliente.post("/providers", data={
        "name": f"{PREFIXO}or", "base_url": "http://x", "capabilities": ["chat"], "active": "on"})
    falso = {"healthy": True, "latency_ms": 12, "message": None}
    with patch("pesquisa_precos.providers.health.sondar_url", return_value=falso) as mock:
        assert cliente.post(f"/providers/{PREFIXO}or/test").status_code == 303
    mock.assert_called_once()
    with db.session() as sessao:
        linha = sessao.execute(text("SELECT healthy, latency_ms FROM provider_status "
                                    "WHERE provider = :n"), {"n": f"{PREFIXO}or"}).first()
    assert linha is not None and linha[0] is True and linha[1] == 12


def test_provedor_de_service_usa_health_e_nao_models(cliente):
    """`pareamento` expõe `/health`, não o `/models` da convenção OpenAI. Desde a ADR-023 é
    o único: `extract` virou um LLM e é sondado como chat."""
    cliente.post("/providers", data={
        "name": f"{PREFIXO}pdf", "base_url": "http://par:8300", "capabilities": ["matching"],
        "active": "on"})
    falso = {"healthy": True, "latency_ms": 5, "message": None}
    with patch("pesquisa_precos.providers.health.sondar_health", return_value=falso) as mock:
        cliente.post(f"/providers/{PREFIXO}pdf/test")
    mock.assert_called_once()


def test_tela_avisa_quando_falta_a_chave_mestra(cliente, monkeypatch):
    """Sem `APP_SECRET_KEY` a tela tem de AVISAR, não explodir — ela é o lugar onde o operador
    vai descobrir o problema."""
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    resp = cliente.get("/providers")
    assert resp.status_code == 200
    assert seg.VAR_CHAVE in resp.text and "não está definida" in resp.text
