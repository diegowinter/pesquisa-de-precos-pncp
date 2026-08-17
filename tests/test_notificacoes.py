"""
Notificações (Fase 9, item 3). Best-effort: nenhuma falha de rede/config pode propagar — é a
propriedade que protege o runner de derrubar uma etapa por causa de um bot do Telegram fora do
ar (docs/04_FASES.md: "best-effort — falha ao notificar não derruba a etapa").
"""

from unittest.mock import patch

import requests

from pesquisa_precos.services import notificacoes


class TestSemCredencial:
    def test_sem_token_ou_chat_id_nao_envia_e_nao_leva_excecao(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert notificacoes.enviar_telegram("teste") is False

    def test_so_token_sem_chat_id_tambem_nao_envia(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert notificacoes.enviar_telegram("teste") is False


class TestEnvioComCredencial:
    def test_envio_bem_sucedido(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            assert notificacoes.enviar_telegram("teste") is True
            assert "123:abc" in mock_post.call_args[0][0]

    def test_erro_http_nao_levanta_excecao(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.return_value.status_code = 401
            mock_post.return_value.text = "unauthorized"
            assert notificacoes.enviar_telegram("teste") is False

    def test_erro_de_rede_nao_levanta_excecao(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("túnel caiu")
            assert notificacoes.enviar_telegram("teste") is False


class TestNotificarEvento:
    def test_evento_desconhecido_e_ignorado(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        assert notificacoes.notificar_evento(1, "3", "evento_que_nao_existe") is False

    def test_evento_valido_monta_mensagem_e_envia(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        with patch("pesquisa_precos.services.notificacoes.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            assert notificacoes.notificar_evento(42, "3", "falhou", detalhe="timeout") is True
            corpo = mock_post.call_args[1]["json"]["text"]
            assert "run #42" in corpo and "etapa 3" in corpo and "timeout" in corpo
