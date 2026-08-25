"""
Sessão HTTP com keep-alive, compartilhada pelos clientes do PNCP.

Por que existe: cada `requests.get` solto abre uma conexão TCP+TLS nova, e um documento da
etapa 2 custa 3+ requests. Reaproveitar a conexão elimina esse handshake.

Honestidade sobre o ganho: medimos os dois modos em 2026-08-23 e o resultado OSCILOU demais
para cravar um número — 4 rodadas deram 26,3 s vs 0,7 s (a favor), 17,7 s vs 167,6 s (contra),
19,1 s vs 39,6 s (contra) e 26,0 s vs 7,3 s (a favor). A variável dominante naquele dia era a
própria API do PNCP, que respondia entre 0,1 s e 60 s para a MESMA URL. Keep-alive continua
sendo o padrão certo (menos handshake, menos chance de tomar reset do WAF), mas não é a cura
da lentidão — a cura seria paralelizar documentos, hoje processados um a um.

A sessão é THREAD-LOCAL: `requests.Session` não é thread-safe, e a etapa 2 chama isto de
dentro do laço de documentos. Uma por thread mantém o keep-alive sem compartilhar estado.
"""

import threading

import requests

_local = threading.local()

# (connect, read). Os dois curtos DE PROPÓSITO: o PNCP responde em menos de 1 s quando
# responde, e um read timeout de 300 s não protege de nada — protege o servidor de nós.
# Em 2026-08-24 as três threads de coleta ficaram HORAS paradas em `socket.recv` esperando
# uma resposta que não vinha, com a etapa viva, o heartbeat batendo e o lock preso. Desistir
# em 60 s e deixar o retry abrir conexão nova é sempre melhor que esperar por educação.
TIMEOUT_PADRAO = (10, 60)


def sessao() -> requests.Session:
    """A `Session` desta thread, criada na primeira chamada."""
    s = getattr(_local, "sessao", None)
    if s is None:
        s = _local.sessao = requests.Session()
    return s


def get(url: str, **kwargs):
    """`requests.get` com keep-alive e o timeout padrão do projeto."""
    kwargs.setdefault("timeout", TIMEOUT_PADRAO)
    return sessao().get(url, **kwargs)
