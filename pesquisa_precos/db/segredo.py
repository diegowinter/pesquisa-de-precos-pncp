"""
Cifra dos segredos que moram no banco (Fase 14, ADR-022).

Até a Fase 13, `provedor.api_key_ref` guardava o *nome* de uma variável de ambiente e o valor
ficava no `.env`. A intenção era não vazar chave em `pg_dump` — mas o efeito colateral era que
cadastrar um provedor pela tela continuava impossível sem editar arquivo e reiniciar o
servidor, ou seja, a tela de provedores nunca virava a superfície de configuração que a ADR-014
prometeu. A chave passa a morar no banco, **cifrada**.

O desenho é envelope simples:

- **chave-mestra** (`APP_SECRET_KEY`) — 32 bytes em base64url, vem do *ambiente do processo*,
  nunca do banco. É a única coisa que continua fora, porque uma chave não pode morar dentro
  do que ela protege.
- **AES-256-GCM** por segredo, com nonce de 12 bytes aleatório por operação. GCM e não CBC
  porque queremos autenticação junto: um `bytea` adulterado no banco falha ao decifrar em vez
  de devolver lixo silencioso.
- O nome do provedor entra como **AAD** (dado autenticado, não cifrado). Isso amarra o
  criptograma à linha: copiar o `api_key_cifrada` do provedor A para o B faz a decifra falhar,
  em vez de o B passar a usar a chave do A sem ninguém notar.

Formato do blob gravado: `b"v1" || key_id (16 bytes, padded) || nonce (12) || ciphertext+tag`.
O `key_id` fica no blob *e* em coluna própria para permitir **rotação** da chave-mestra sem
downtime: durante a janela, `APP_SECRET_KEY_ANTIGA` também é aceita na decifra, e
`recifrar` reescreve linha a linha.

Nada aqui loga, devolve ou formata o segredo em claro. Quem exibe usa `ultimos4`.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIXO = b"v1"
_TAM_KEY_ID = 16
_TAM_NONCE = 12

VAR_CHAVE = "APP_SECRET_KEY"
VAR_CHAVE_ANTIGA = "APP_SECRET_KEY_ANTIGA"


class ChaveMestraAusente(RuntimeError):
    """`APP_SECRET_KEY` não está no ambiente. Sem ela não há como cifrar nem decifrar chave de
    provedor — e, desde a ADR-022, não há provedor sem chave. Falha alta e clara, na mesma
    linha do que a ADR-020 fez com `DATABASE_URL`: um caminho só, sem modo degradado."""


class SegredoInvalido(ValueError):
    """O blob não decifra: chave-mestra errada (ou rotacionada sem re-cifrar), blob truncado,
    ou criptograma movido de uma linha para outra (o AAD não bate)."""


def _material(valor: str) -> bytes:
    """Aceita a chave-mestra como base64url de 32 bytes (o formato que `gerar_chave_mestra`
    emite) ou como texto livre, derivando 32 bytes por SHA-256. A segunda forma existe porque
    operador vai colar senha à mão em algum momento — melhor derivar do que recusar e ver a
    chave virar um `APP_SECRET_KEY=123` improvisado noutro lugar."""
    try:
        bruto = base64.urlsafe_b64decode(valor.encode("ascii"))
        if len(bruto) == 32:
            return bruto
    except (ValueError, UnicodeEncodeError):
        pass
    return hashlib.sha256(valor.encode("utf-8")).digest()


def gerar_chave_mestra() -> str:
    """Uma `APP_SECRET_KEY` nova, pronta para colar no ambiente. Não persiste nada."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _key_id(material: bytes) -> str:
    """Identificador público da chave-mestra: 8 bytes do SHA-256 dela, em hex. Não revela a
    chave e permite saber qual delas cifrou cada linha durante uma rotação."""
    return hashlib.sha256(material).hexdigest()[:16]


def _chave_atual() -> bytes:
    valor = os.getenv(VAR_CHAVE, "")
    if not valor:
        raise ChaveMestraAusente(
            f"{VAR_CHAVE} não está definida no ambiente. Gere uma com "
            f"`python -c \"from pesquisa_precos.db import segredo; "
            f"print(segredo.gerar_chave_mestra())\"` e defina-a como variável de ambiente do "
            f"serviço (ou no .env, em desenvolvimento). Sem ela não é possível ler nem gravar "
            f"a chave de API dos provedores (ADR-022).")
    return _material(valor)


def _chaves_aceitas() -> list[bytes]:
    """Atual primeiro, antiga depois — a ordem importa: a decifra tenta em sequência e o caso
    comum (tudo já re-cifrado) resolve na primeira."""
    chaves = [_chave_atual()]
    antiga = os.getenv(VAR_CHAVE_ANTIGA, "")
    if antiga:
        chaves.append(_material(antiga))
    return chaves


def key_id_atual() -> str:
    return _key_id(_chave_atual())


def configurada() -> bool:
    """Para a tela de diagnóstico dizer 'chave-mestra ausente' sem derrubar a página."""
    return bool(os.getenv(VAR_CHAVE, ""))


def cifrar(segredo: str, *, contexto: str) -> bytes:
    """`contexto` é o AAD — na prática, `provedor.nome`. Ver docstring do módulo."""
    material = _chave_atual()
    kid = _key_id(material).encode("ascii").ljust(_TAM_KEY_ID, b"\x00")
    nonce = os.urandom(_TAM_NONCE)
    cripto = AESGCM(material).encrypt(nonce, segredo.encode("utf-8"), contexto.encode("utf-8"))
    return _PREFIXO + kid + nonce + cripto


def decifrar(blob: bytes, *, contexto: str) -> str:
    """Devolve o segredo em claro. Só `providers/resolver` deve chamar isto, e só para montar
    o adapter — o valor nunca sobe para a API nem para o HTML (ADR-022)."""
    if not blob or not blob.startswith(_PREFIXO):
        raise SegredoInvalido("blob de segredo com formato desconhecido (esperado prefixo v1)")
    corpo = blob[len(_PREFIXO):]
    if len(corpo) < _TAM_KEY_ID + _TAM_NONCE + 16:
        raise SegredoInvalido("blob de segredo truncado")
    nonce = corpo[_TAM_KEY_ID:_TAM_KEY_ID + _TAM_NONCE]
    cripto = corpo[_TAM_KEY_ID + _TAM_NONCE:]
    aad = contexto.encode("utf-8")
    for material in _chaves_aceitas():
        try:
            return AESGCM(material).decrypt(nonce, cripto, aad).decode("utf-8")
        except InvalidTag:
            continue
    raise SegredoInvalido(
        f"não foi possível decifrar o segredo de {contexto!r} com a(s) chave-mestra(s) "
        f"disponível(is). Se a {VAR_CHAVE} foi trocada, defina a anterior em "
        f"{VAR_CHAVE_ANTIGA} e re-cifre as linhas antes de removê-la.")


def key_id_do_blob(blob: bytes) -> str | None:
    """Qual chave-mestra cifrou este blob, sem tentar decifrar — é o que permite listar o que
    ainda falta re-cifrar numa rotação."""
    if not blob or not blob.startswith(_PREFIXO):
        return None
    kid = blob[len(_PREFIXO):len(_PREFIXO) + _TAM_KEY_ID]
    return kid.rstrip(b"\x00").decode("ascii", errors="replace") or None


def recifrar(blob: bytes, *, contexto: str) -> bytes:
    """Decifra com qualquer chave aceita e cifra de novo com a atual (rotação)."""
    return cifrar(decifrar(blob, contexto=contexto), contexto=contexto)


def ultimos4(segredo: str) -> str:
    """O que a tela mostra no lugar da chave. Segredos curtos não expõem nada."""
    return segredo[-4:] if len(segredo) >= 8 else ""
