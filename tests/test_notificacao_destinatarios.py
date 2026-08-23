"""
CRUD de `notification_recipient` (Fase 9, pedido do usuário em 2026-08-17) e canal de e-mail
via Resend. Repositório/serviço seguem o mesmo padrão de `tests/test_config_prompts.py`: pulados
sem Postgres disponível. O envio via Resend é mockado — nunca bate na rede de verdade, mesmo
padrão de `tests/test_notifications.py`.
"""

from unittest.mock import patch

import pytest
import requests

from pesquisa_precos.db import session as db
from pesquisa_precos.services import notification_recipients as service
from pesquisa_precos.services.notification_recipients import (
    DestinatarioInexistente,
    DestinatarioSemCanal,
)

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)


@pytest.fixture(autouse=True)
def _destinatarios_de_teste_limpos():
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "DELETE FROM notification_recipient WHERE name LIKE 'teste-fase9-%'"))
    yield
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "DELETE FROM notification_recipient WHERE name LIKE 'teste-fase9-%'"))


# ── validação de canal (pura, sem banco) ─────────────────────────────────────────────

def test_criar_sem_email_levanta():
    with pytest.raises(DestinatarioSemCanal):
        service.criar_destinatario("teste-fase9-sem-canal", None)


def test_criar_com_email_e_valido():
    with pytest.raises(DestinatarioSemCanal):
        service._validar_canal(None)  # sanity: helper interno usado por criar/edit
    service._validar_canal("a@b.com")  # não levanta


# ── CRUD completo (precisa de Postgres) ──────────────────────────────────────────────

@pytestmark_db
def test_criar_listar_e_obter_destinatario():
    destinatario_id = service.criar_destinatario("teste-fase9-crud", "a@b.com")
    destinatario = service.obter_destinatario(destinatario_id)
    assert destinatario["name"] == "teste-fase9-crud"
    assert destinatario["email"] == "a@b.com"
    assert destinatario["active"] is True
    assert any(d["id"] == destinatario_id for d in service.listar_destinatarios())


@pytestmark_db
def test_editar_destinatario_atualiza_campos():
    destinatario_id = service.criar_destinatario("teste-fase9-editar", "a@b.com")
    service.editar_destinatario(destinatario_id, "teste-fase9-editar-2", "c@d.com")
    destinatario = service.obter_destinatario(destinatario_id)
    assert destinatario["name"] == "teste-fase9-editar-2"
    assert destinatario["email"] == "c@d.com"


@pytestmark_db
def test_editar_sem_canal_levanta_e_nao_altera_nada():
    destinatario_id = service.criar_destinatario("teste-fase9-editar-invalido", "a@b.com")
    with pytest.raises(DestinatarioSemCanal):
        service.editar_destinatario(destinatario_id, "x", None)
    assert service.obter_destinatario(destinatario_id)["email"] == "a@b.com"


@pytestmark_db
def test_editar_id_inexistente_levanta():
    with pytest.raises(DestinatarioInexistente):
        service.editar_destinatario(999_999_999, "x", "a@b.com")


@pytestmark_db
def test_desativar_e_ativar_destinatario():
    destinatario_id = service.criar_destinatario("teste-fase9-toggle", "a@b.com")
    service.desativar_destinatario(destinatario_id)
    assert service.obter_destinatario(destinatario_id)["active"] is False
    assert not any(d["id"] == destinatario_id
                   for d in service.listar_destinatarios(apenas_ativos=True))
    service.ativar_destinatario(destinatario_id)
    assert service.obter_destinatario(destinatario_id)["active"] is True


@pytestmark_db
def test_desativar_id_inexistente_levanta():
    with pytest.raises(DestinatarioInexistente):
        service.desativar_destinatario(999_999_999)


# ── envio via Resend (mockado — nunca bate na rede) ──────────────────────────────────

class TestEnvioResend:
    def test_sem_credencial_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_so_api_key_sem_remetente_tambem_nao_envia(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_envio_bem_sucedido(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is True
            chamada = mock_post.call_args
            assert chamada[0][0] == notifications.RESEND_API
            assert chamada[1]["headers"]["Authorization"] == "Bearer re_123"
            assert chamada[1]["json"]["from"] == "pesquisa-precos@dominio.com"
            assert chamada[1]["json"]["to"] == ["a@b.com"]

    def test_erro_http_nao_levanta_excecao(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
            mock_post.return_value.status_code = 422
            mock_post.return_value.text = "invalid from"
            assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_erro_de_rede_nao_levanta_excecao(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("falha de rede")
            assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False


class TestNotificarEventoComDestinatarios:
    def test_sem_destinatarios_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications._destinatarios_ativos",
                   return_value=[]):
            assert notifications.notificar_evento(1, "3", "finished") is False

    def test_com_destinatarios_envia_para_cada_um(self, monkeypatch):
        from pesquisa_precos.services import notifications
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        destinatarios = [
            {"id": 1, "name": "A", "email": "a@b.com", "active": True},
            {"id": 2, "name": "B", "email": "b@b.com", "active": True},
        ]
        with patch("pesquisa_precos.services.notifications._destinatarios_ativos",
                   return_value=destinatarios):
            with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                assert notifications.notificar_evento(1, "3", "failed", detalhe="timeout") is True
                assert mock_post.call_count == 2
