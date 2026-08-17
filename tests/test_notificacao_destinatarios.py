"""
CRUD de `notificacao_destinatario` (Fase 9, pedido do usuário em 2026-08-17) e canal de e-mail
via Resend. Repositório/serviço seguem o mesmo padrão de `tests/test_config_prompts.py`: pulados
sem Postgres disponível. O envio via Resend é mockado — nunca bate na rede de verdade, mesmo
padrão de `tests/test_notificacoes.py`.
"""

from unittest.mock import patch

import pytest
import requests

from pesquisa_precos.db import sessao as db
from pesquisa_precos.services import notificacao_destinatarios as servico
from pesquisa_precos.services.notificacao_destinatarios import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.url_banco()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.esta_disponivel()[0], reason=_MOTIVO_SEM_BANCO)


@pytest.fixture(autouse=True)
def _destinatarios_de_teste_limpos():
    if db.esta_disponivel()[0]:
        with db.sessao() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "DELETE FROM notificacao_destinatario WHERE nome LIKE 'teste-fase9-%'"))
    yield
    if db.esta_disponivel()[0]:
        with db.sessao() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "DELETE FROM notificacao_destinatario WHERE nome LIKE 'teste-fase9-%'"))


# ── validação de canal (pura, sem banco) ─────────────────────────────────────────────

def test_criar_sem_email_levanta():
    with pytest.raises(DestinatarioSemCanal):
        servico.criar_destinatario("teste-fase9-sem-canal", None)


def test_criar_com_email_e_valido():
    with pytest.raises(DestinatarioSemCanal):
        servico._validar_canal(None)  # sanity: helper interno usado por criar/editar
    servico._validar_canal("a@b.com")  # não levanta


# ── CRUD completo (precisa de Postgres) ──────────────────────────────────────────────

@pytestmark_db
def test_criar_listar_e_obter_destinatario():
    destinatario_id = servico.criar_destinatario("teste-fase9-crud", "a@b.com")
    destinatario = servico.obter_destinatario(destinatario_id)
    assert destinatario["nome"] == "teste-fase9-crud"
    assert destinatario["email"] == "a@b.com"
    assert destinatario["ativo"] is True
    assert any(d["id"] == destinatario_id for d in servico.listar_destinatarios())


@pytestmark_db
def test_editar_destinatario_atualiza_campos():
    destinatario_id = servico.criar_destinatario("teste-fase9-editar", "a@b.com")
    servico.editar_destinatario(destinatario_id, "teste-fase9-editar-2", "c@d.com")
    destinatario = servico.obter_destinatario(destinatario_id)
    assert destinatario["nome"] == "teste-fase9-editar-2"
    assert destinatario["email"] == "c@d.com"


@pytestmark_db
def test_editar_sem_canal_levanta_e_nao_altera_nada():
    destinatario_id = servico.criar_destinatario("teste-fase9-editar-invalido", "a@b.com")
    with pytest.raises(DestinatarioSemCanal):
        servico.editar_destinatario(destinatario_id, "x", None)
    assert servico.obter_destinatario(destinatario_id)["email"] == "a@b.com"


@pytestmark_db
def test_editar_id_inexistente_levanta():
    with pytest.raises(DestinatarioInexistente):
        servico.editar_destinatario(999_999_999, "x", "a@b.com")


@pytestmark_db
def test_desativar_e_ativar_destinatario():
    destinatario_id = servico.criar_destinatario("teste-fase9-toggle", "a@b.com")
    servico.desativar_destinatario(destinatario_id)
    assert servico.obter_destinatario(destinatario_id)["ativo"] is False
    assert not any(d["id"] == destinatario_id
                   for d in servico.listar_destinatarios(apenas_ativos=True))
    servico.ativar_destinatario(destinatario_id)
    assert servico.obter_destinatario(destinatario_id)["ativo"] is True


@pytestmark_db
def test_desativar_id_inexistente_levanta():
    with pytest.raises(DestinatarioInexistente):
        servico.desativar_destinatario(999_999_999)


# ── envio via Resend (mockado — nunca bate na rede) ──────────────────────────────────

class TestEnvioResend:
    def test_sem_credencial_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notificacoes.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_so_api_key_sem_remetente_tambem_nao_envia(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notificacoes.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_envio_bem_sucedido(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            assert notificacoes.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is True
            chamada = mock_post.call_args
            assert chamada[0][0] == notificacoes.RESEND_API
            assert chamada[1]["headers"]["Authorization"] == "Bearer re_123"
            assert chamada[1]["json"]["from"] == "pesquisa-precos@dominio.com"
            assert chamada[1]["json"]["to"] == ["a@b.com"]

    def test_erro_http_nao_levanta_excecao(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.return_value.status_code = 422
            mock_post.return_value.text = "invalid from"
            assert notificacoes.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_erro_de_rede_nao_levanta_excecao(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("falha de rede")
            assert notificacoes.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False


class TestNotificarEventoComDestinatarios:
    def test_sem_destinatarios_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notificacoes._destinatarios_ativos",
                   return_value=[]):
            assert notificacoes.notificar_evento(1, "3", "concluida") is False

    def test_com_destinatarios_envia_para_cada_um(self, monkeypatch):
        from pesquisa_precos.services import notificacoes
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        destinatarios = [
            {"id": 1, "nome": "A", "email": "a@b.com", "ativo": True},
            {"id": 2, "nome": "B", "email": "b@b.com", "ativo": True},
        ]
        with patch("pesquisa_precos.services.notificacoes._destinatarios_ativos",
                   return_value=destinatarios):
            with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                assert notificacoes.notificar_evento(1, "3", "falhou", detalhe="timeout") is True
                assert mock_post.call_count == 2
