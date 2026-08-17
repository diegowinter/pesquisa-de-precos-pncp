"""
Notificações (Fase 9, docs/04_FASES.md item 3) — etapa concluída / falhou / gate aguardando.
"Pipeline em background sem aviso vira pipeline esquecido" (docs/06_API_E_WEB.md §6).

Canal implementado: e-mail via Resend (`POST https://api.resend.com/emails`, API HTTP simples —
sem SMTP). Telegram foi removido (pedido do usuário em 2026-08-17: "vamos tirar a parte do
telegram, é melhor, deixa só o resend mesmo, menos complexidade").

Credenciais só em `.env` (`RESEND_API_KEY`, `RESEND_FROM_EMAIL`), NUNCA no banco
(docs/08_CONVENCOES.md §5.10, mesmo princípio de `provedor.api_key_ref`). QUEM recebe (lista de
e-mails) vem da tabela `notificacao_destinatario`, com CRUD próprio na interface web.

BEST-EFFORT por desenho: qualquer falha de rede/config aqui é engolida e logada — uma
notificação que não saiu não pode derrubar a etapa nem o processo que a chamou (ADR-001: este
é um sistema de um operador; a notificação é conveniência, não parte do contrato de dados).
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"
TIMEOUT_S = 10

# Eventos que disparam notificação — bate com os três do critério de aceite da Fase 9
# ("etapa concluída / falhou / gate aguardando ... em menos de 1 min").
EVENTOS = ("concluida", "falhou", "aguardando_aprovacao")


def _resend_configurado() -> tuple[str, str] | None:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    remetente = os.getenv("RESEND_FROM_EMAIL", "").strip()
    if not api_key or not remetente:
        return None
    return api_key, remetente


def enviar_resend(destinatario: str, assunto: str, html: str) -> bool:
    """Envia e-mail via API HTTP do Resend (`POST /emails`). Devolve `True`/`False` — nunca
    levanta (best-effort). `False` sem exceção quando `RESEND_API_KEY`/`RESEND_FROM_EMAIL` não
    estão configurados no `.env`."""
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
    """Monta e envia a mensagem de um evento de `run_etapa` (Fase 9, item 3) por e-mail (Resend)
    para todos os destinatários ativos cadastrados em `notificacao_destinatario`. `evento` é um
    dos três de `EVENTOS`; qualquer outro valor é ignorado silenciosamente (defensivo — não é
    para o runner ter que saber a lista aqui, mas também não pode notificar lixo). Devolve
    `True` se ao menos um envio teve sucesso. Sem destinatário cadastrado (ou nenhum envio bem
    sucedido), loga e devolve `False`."""
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
    except Exception:  # noqa: BLE001 — banco indisponível não pode propagar (best-effort)
        logger.exception("falha ao carregar destinatários de notificação")
        return False

    if not destinatarios:
        logger.info("notificação pulada: nenhum destinatário ativo cadastrado")
        return False

    try:
        algum_sucesso = False
        for destinatario in destinatarios:
            if destinatario.get("email") and enviar_resend(destinatario["email"], assunto, html):
                algum_sucesso = True
        return algum_sucesso
    except Exception:  # noqa: BLE001 — best-effort: notificação nunca pode propagar (ver docstring)
        logger.exception("falha inesperada ao notificar run #%s etapa %s", run_id, etapa)
        return False
