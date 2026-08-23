"""
Notificações (Fase 9, item 3). Best-effort: nenhuma falha de rede/config pode propagar — é a
propriedade que protege o runner de derrubar uma step por causa do Resend fora do ar
(docs/04_FASES.md: "best-effort — falha ao notificar não derruba a step").
"""

from unittest.mock import patch

import requests

from pesquisa_precos.services import notifications


class TestEnvioResend:
    def test_sem_credencial_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_so_api_key_sem_remetente_tambem_nao_envia(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_envio_bem_sucedido(self, monkeypatch):
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
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
            mock_post.return_value.status_code = 422
            mock_post.return_value.text = "invalid from"
            assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False

    def test_erro_de_rede_nao_levanta_excecao(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("falha de rede")
            assert notifications.enviar_resend("a@b.com", "assunto", "<p>oi</p>") is False


class TestNotificarEvento:
    def test_evento_desconhecido_e_ignorado(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        assert notifications.notificar_evento(1, "3", "evento_que_nao_existe") is False

    def test_sem_destinatarios_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_123")
        monkeypatch.setenv("RESEND_FROM_EMAIL", "pesquisa-precos@dominio.com")
        with patch("pesquisa_precos.services.notifications._destinatarios_ativos",
                   return_value=[]):
            assert notifications.notificar_evento(1, "3", "finished") is False

    def test_com_destinatarios_envia_para_cada_um(self, monkeypatch):
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
                assert notifications.notificar_evento(42, "3", "failed", detalhe="timeout") is True
                assert mock_post.call_count == 2
                corpo = mock_post.call_args_list[0][1]["json"]["html"]
                assert "run #42" in corpo and "step 3" in corpo and "timeout" in corpo
