"""
Rotas HTML de `/provedores` (Fase 14 bloco 2, ADR-022).

Subir a app num `TestClient` não é executar etapa (CLAUDE.md, regra nº 1): nenhuma rota aqui
dispara `POST .../executar` nem `.../aprovar`. O `testar` de provedor é sondagem HTTP e está
mockado — nunca bate na rede de verdade, mesmo padrão de `tests/test_notificacoes.py`.

O teste que mais importa é `test_chave_nunca_aparece_no_html`: se ele cair, a razão que
permitiu a chave sair do `.env` deixou de valer.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from pesquisa_precos.db import segredo as seg
from pesquisa_precos.db import sessao as db
from pesquisa_precos.services import provedores as servico

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.url_banco()} — rode `alembic upgrade head` antes"
pytestmark = pytest.mark.skipif(not db.esta_disponivel()[0], reason=_MOTIVO_SEM_BANCO)

PREFIXO = "teste-f14w-"
CHAVE = "sk-or-v1-nao-pode-vazar"


def _limpar():
    with db.sessao() as sessao:
        sessao.execute(text("DELETE FROM capacidade_provedor WHERE provedor LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provedor_status WHERE provedor LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provedor WHERE nome LIKE :p"), {"p": f"{PREFIXO}%"})


def _snapshot_capacidades():
    """Fotografa `capacidade_provedor` inteira.

    Os testes apontam capacidades REAIS (`chat`, `embed`, ...) para provedores fictícios — não
    há capacidade "de teste", o enum é fechado. Sem restaurar depois, rodar `pytest` apagaria a
    configuração de produção do operador: as etapas parariam de resolver provedor e a culpa
    pareceria do código. Já aconteceu uma vez.
    """
    with db.sessao() as sessao:
        return [dict(r) for r in sessao.execute(text(
            "SELECT capacidade, provedor, modelo, fallback FROM capacidade_provedor"
        )).mappings()]


def _restaurar_capacidades(linhas):
    from pesquisa_precos.db.repos import execucao as repo

    with db.sessao() as sessao:
        sessao.execute(text("DELETE FROM capacidade_provedor"))
        for linha in linhas:
            repo.apontar_capacidade(sessao, linha["capacidade"], linha["provedor"],
                                    linha["modelo"], linha["fallback"])


@pytest.fixture
def cliente(monkeypatch):
    from fastapi.testclient import TestClient

    from pesquisa_precos.web.app import app

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.delenv("WEB_SENHA", raising=False)   # login desligado, como local
    capacidades = _snapshot_capacidades()
    _limpar()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    _limpar()
    _restaurar_capacidades(capacidades)


def test_criar_provedor_pela_tela(cliente):
    resp = cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "https://openrouter.ai/api/v1",
        "capacidades": ["chat"], "modelo_padrao": "modelo-barato",
        "batch_size": "16", "custo_in_por_mtok": "0.01", "ativo": "on", "api_key": CHAVE})
    assert resp.status_code == 303
    p = servico.obter(f"{PREFIXO}or")
    assert p["base_url"] == "https://openrouter.ai/api/v1"
    assert p["batch_size"] == 16 and p["tem_api_key"] is True


def test_campos_numericos_vazios_nao_quebram(cliente):
    """Campo numérico de formulário HTML chega como `''`, não `None`."""
    resp = cliente.post("/provedores", data={
        "nome": f"{PREFIXO}gpu", "base_url": "http://gpu:8100", "capacidades": ["embed"],
        "batch_size": "", "rpm_limite": "", "custo_in_por_mtok": "", "ativo": "on"})
    assert resp.status_code == 303
    p = servico.obter(f"{PREFIXO}gpu")
    assert p["rpm_limite"] is None and p["custo_in_por_mtok"] is None


def test_chave_nunca_aparece_no_html(cliente):
    """A promessa da ADR-022: a chave entra e não volta. Nem na listagem, nem no formulário
    de edição, nem num `value=` de input."""
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"],
        "ativo": "on", "api_key": CHAVE})

    html = cliente.get("/provedores").text
    assert CHAVE not in html and "nao-pode-vazar" not in html
    assert "azar" in html            # os 4 últimos dígitos, esses sim

    html_edicao = cliente.get(f"/provedores?editar={PREFIXO}or").text
    assert CHAVE not in html_edicao
    assert 'name="api_key"' in html_edicao          # o campo existe...
    assert f'value="{CHAVE}"' not in html_edicao    # ...e nasce vazio


def test_editar_sem_preencher_a_chave_mantem_a_gravada(cliente):
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"],
        "ativo": "on", "api_key": CHAVE})
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://novo", "capacidades": ["chat"],
        "ativo": "on", "api_key": ""})
    p = servico.obter(f"{PREFIXO}or")
    assert p["base_url"] == "http://novo" and p["tem_api_key"] is True


def test_limpar_chave_pela_tela(cliente):
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"],
        "ativo": "on", "api_key": CHAVE})
    assert cliente.post(f"/provedores/{PREFIXO}or/chave/limpar").status_code == 303
    assert servico.obter(f"{PREFIXO}or")["tem_api_key"] is False


def test_formulario_invalido_volta_com_erro(cliente):
    resp = cliente.post("/provedores", data={
        "nome": f"{PREFIXO}x", "base_url": "", "capacidades": ["chat"], "ativo": "on"})
    assert resp.status_code == 303
    assert "erro=" in resp.headers["location"]
    assert servico.obter(f"{PREFIXO}x") is None


def test_apontar_capacidade_pela_tela(cliente):
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"], "ativo": "on"})
    resp = cliente.post("/provedores/capacidades",
                        data={"capacidade": "chat", "provedor": f"{PREFIXO}or",
                              "modelo": "modelo-x", "fallback": ""})
    assert resp.status_code == 303 and "erro=" not in resp.headers["location"]


def test_fallback_em_embed_recusado_pela_tela(cliente):
    for sufixo in ("a", "b"):
        cliente.post("/provedores", data={
            "nome": f"{PREFIXO}{sufixo}", "base_url": f"http://{sufixo}",
            "capacidades": ["embed"], "ativo": "on"})
    resp = cliente.post("/provedores/capacidades",
                        data={"capacidade": "embed", "provedor": f"{PREFIXO}a",
                              "fallback": f"{PREFIXO}b"})
    assert resp.status_code == 303 and "erro=" in resp.headers["location"]


def test_ativar_desativar_pela_tela(cliente):
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"], "ativo": "on"})
    cliente.post(f"/provedores/{PREFIXO}or/ativo", data={"ativo": "off"})
    assert servico.obter(f"{PREFIXO}or")["ativo"] is False
    cliente.post(f"/provedores/{PREFIXO}or/ativo", data={"ativo": "on"})
    assert servico.obter(f"{PREFIXO}or")["ativo"] is True


def test_testar_provedor_grava_status(cliente):
    """Sondagem mockada: o teste é sobre gravar `provedor_status`, não sobre a rede."""
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}or", "base_url": "http://x", "capacidades": ["chat"], "ativo": "on"})
    falso = {"saudavel": True, "latencia_ms": 12, "mensagem": None}
    with patch("pesquisa_precos.providers.saude.sondar_url", return_value=falso) as mock:
        assert cliente.post(f"/provedores/{PREFIXO}or/testar").status_code == 303
    mock.assert_called_once()
    with db.sessao() as sessao:
        linha = sessao.execute(text("SELECT saudavel, latencia_ms FROM provedor_status "
                                    "WHERE provedor = :n"), {"n": f"{PREFIXO}or"}).first()
    assert linha is not None and linha[0] is True and linha[1] == 12


def test_provedor_de_servico_usa_health_e_nao_models(cliente):
    """`pdf`/`pareamento` expõem `/health`, não o `/models` da convenção OpenAI."""
    cliente.post("/provedores", data={
        "nome": f"{PREFIXO}pdf", "base_url": "http://pdf:8200", "capacidades": ["pdf"],
        "ativo": "on"})
    falso = {"saudavel": True, "latencia_ms": 5, "mensagem": None}
    with patch("pesquisa_precos.providers.saude.sondar_health", return_value=falso) as mock:
        cliente.post(f"/provedores/{PREFIXO}pdf/testar")
    mock.assert_called_once()


def test_tela_avisa_quando_falta_a_chave_mestra(cliente, monkeypatch):
    """Sem `APP_SECRET_KEY` a tela tem de AVISAR, não explodir — ela é o lugar onde o operador
    vai descobrir o problema."""
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    resp = cliente.get("/provedores")
    assert resp.status_code == 200
    assert seg.VAR_CHAVE in resp.text and "não está definida" in resp.text
