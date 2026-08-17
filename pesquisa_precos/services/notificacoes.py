"""
Notificações (Fase 9, docs/04_FASES.md item 3) — etapa concluída / falhou / gate aguardando.
"Pipeline em background sem aviso vira pipeline esquecido" (docs/06_API_E_WEB.md §6).

Canais implementados:
- Telegram (bot API via HTTP).
- E-mail via Resend (`POST https://api.resend.com/emails`, API HTTP simples — sem SMTP).

Credenciais só em `.env` (`TELEGRAM_BOT_TOKEN`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`), NUNCA
no banco (docs/08_CONVENCOES.md §5.10, mesmo princípio de `provedor.api_key_ref`). QUEM recebe
(lista de e-mails/chat_ids) já não vem mais de variável fixa de `.env` — vem da tabela
`notificacao_destinatario`, com CRUD próprio na interface web (pedido do usuário em
2026-08-17). `TELEGRAM_CHAT_ID` no `.env` continua funcionando como fallback best-effort caso
nenhum destinatário esteja cadastrado ainda (evita regressão para quem já tinha configurado só
por `.env`).

BEST-EFFORT por desenho: qualquer falha de rede/config aqui é engolida e logada — uma
notificação que não saiu não pode derrubar a etapa nem o processo que a chamou (ADR-001: este
é um sistema de um operador; a notificação é conveniência, não parte do contrato de dados).
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
RESEND_API = "https://api.resend.com/emails"
TIMEOUT_S = 10

# Eventos que disparam notificação — bate com os três do critério de aceite da Fase 9
# ("etapa concluída / falhou / gate aguardando ... em menos de 1 min").
EVENTOS = ("concluida", "falhou", "aguardando_aprovacao")


def _telegram_configurado(*, exige_chat_id_env: bool) -> tuple[str, str] | None:
    """`exige_chat_id_env=False` quando quem chama já traz um `chat_id` explícito (destinatário
    cadastrado) — nesse caso só o token do bot é obrigatório. `True` no caminho de fallback, que
    depende do `TELEGRAM_CHAT_ID` do `.env`."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or (exige_chat_id_env and not chat_id):
        return None
    return token, chat_id


def enviar_telegram(mensagem: str, chat_id: str | None = None) -> bool:
    """Envia `mensagem` para `chat_id` (ou o do `.env` se omitido). Devolve `True`/`False` —
    nunca levanta (best-effort). `False` sem exceção quando não há credencial configurada
    (estado normal enquanto o operador não configurou o bot; não é erro)."""
    cred = _telegram_configurado(exige_chat_id_env=chat_id is None)
    if cred is None:
        logger.info("notificação Telegram pulada: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes")
        return False
    token, chat_id_padrao = cred
    destino = chat_id or chat_id_padrao
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": destino, "text": mensagem, "parse_mode": "HTML"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code >= 400:
            logger.warning("notificação Telegram falhou (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("notificação Telegram falhou (rede): %s", exc)
        return False


def _resend_configurado() -> tuple[str, str] | None:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    remetente = os.getenv("RESEND_FROM_EMAIL", "").strip()
    if not api_key or not remetente:
        return None
    return api_key, remetente


def enviar_resend(destinatario: str, assunto: str, html: str) -> bool:
    """Envia e-mail via API HTTP do Resend (`POST /emails`). Devolve `True`/`False` — nunca
    levanta (best-effort, mesmo padrão de `enviar_telegram`). `False` sem exceção quando
    `RESEND_API_KEY`/`RESEND_FROM_EMAIL` não estão configurados no `.env`."""
    cred = _resend_configurado()
    if cred is None:
        logger.info("notificação Resend pulada: RESEND_API_KEY/RESEND_FROM_EMAIL ausentes")
        return False
    api_key, remetente = cred
    try:
        resp = requests.post(
            RESEND_API,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": remetente, "to": [destinatario], "subject": assunto, "html": html},
            timeout=TIMEOUT_S,
        )
        if resp.status_code >= 400:
            logger.warning("notificação Resend falhou (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("notificação Resend falhou (rede): %s", exc)
        return False


_ROTULO_EVENTO = {
    "concluida": "✅ concluída",
    "falhou": "❌ falhou",
    "aguardando_aprovacao": "⏸ aguardando aprovação",
}


def _destinatarios_ativos() -> list[dict]:
    """Busca `notificacao_destinatario` ativos. Import local (não no topo do módulo) para não
    criar dependência de banco disponível só para importar `notificacoes` — usado em contexto
    de subprocesso da etapa, onde falhar cedo por causa de notificação seria pior que só não
    notificar (mesmo espírito best-effort do resto do módulo)."""
    from pesquisa_precos.services import notificacao_destinatarios as servico_destinatarios

    return servico_destinatarios.listar_destinatarios(apenas_ativos=True)


def notificar_evento(run_id: int, etapa: str, evento: str, *, detalhe: str = "") -> bool:
    """Monta e envia a mensagem de um evento de `run_etapa` (Fase 9, item 3) para todos os
    destinatários ativos cadastrados em `notificacao_destinatario` — Telegram (`telegram_chat_id`)
    e e-mail via Resend (`email`). `evento` é um dos três de `EVENTOS`; qualquer outro valor é
    ignorado silenciosamente (defensivo — não é para o runner ter que saber a lista aqui, mas
    também não pode notificar lixo). Devolve `True` se ao menos um envio teve sucesso.

    Sem nenhum destinatário cadastrado ainda, cai no fallback de `TELEGRAM_CHAT_ID` do `.env`
    (compatibilidade com quem já tinha configurado só por variável de ambiente antes do CRUD)."""
    if evento not in EVENTOS:
        return False
    titulo = _ROTULO_EVENTO.get(evento, evento)
    mensagem = f"Pesquisa de Preços PLASEG — run #{run_id}, etapa {etapa}: {titulo}"
    if detalhe:
        mensagem += f"\n{detalhe[:500]}"
    assunto = f"Pesquisa de Preços PLASEG — run #{run_id}, etapa {etapa}: {titulo}"
    html = f"<p>{mensagem.replace(chr(10), '<br>')}</p>"

    try:
        destinatarios = _destinatarios_ativos()
    except Exception:  # noqa: BLE001 — banco indisponível não pode impedir o fallback best-effort
        logger.exception("falha ao carregar destinatários; usando fallback de .env")
        destinatarios = []

    try:
        if not destinatarios:
            return enviar_telegram(mensagem)
        algum_sucesso = False
        for destinatario in destinatarios:
            if destinatario.get("telegram_chat_id"):
                if enviar_telegram(mensagem, chat_id=destinatario["telegram_chat_id"]):
                    algum_sucesso = True
            if destinatario.get("email"):
                if enviar_resend(destinatario["email"], assunto, html):
                    algum_sucesso = True
        return algum_sucesso
    except Exception:  # noqa: BLE001 — best-effort: notificação nunca pode propagar (ver docstring)
        logger.exception("falha inesperada ao notificar run #%s etapa %s", run_id, etapa)
        return False
