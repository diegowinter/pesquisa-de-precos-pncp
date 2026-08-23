"""
Cifra dos segredos no banco (Fase 14, ADR-022) — `db/segredo.py`.

Sem banco: tudo aqui é in-process. O que estes testes protegem é a razão de a key poder sair
do `.env`: se qualquer uma destas propriedades cair, guardar a key no banco vira risco em vez
de conveniência.
"""

import base64
import importlib
import pkgutil
from pathlib import Path

import pytest

from pesquisa_precos.db import secret as seg


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.delenv(seg.VAR_CHAVE_ANTIGA, raising=False)


def test_roundtrip(key):
    blob = seg.cifrar("sk-or-v1-segredo", context="openrouter")
    assert seg.decifrar(blob, context="openrouter") == "sk-or-v1-segredo"


def test_criptograma_nao_contem_o_segredo_em_claro(key):
    """O reason de existir: um `pg_dump` do `bytea` não pode conter a key legível."""
    blob = seg.cifrar("sk-or-v1-segredo", context="openrouter")
    assert b"sk-or" not in blob and b"segredo" not in blob


def test_nonce_novo_a_cada_cifra(key):
    """Reusar nonce em GCM quebra a cifra inteira — dois criptogramas do mesmo texto têm de
    diferir."""
    a = seg.cifrar("mesma-key", context="p")
    b = seg.cifrar("mesma-key", context="p")
    assert a != b
    assert seg.decifrar(a, context="p") == seg.decifrar(b, context="p")


def test_aad_amarra_o_blob_ao_provedor(key):
    """Copiar `api_key_encrypted` do provider A para o B tem de FALHAR, não fazer o B usar a
    key do A em silêncio."""
    blob = seg.cifrar("key-do-a", context="provedor_a")
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(blob, context="provedor_b")


def test_blob_adulterado_falha(key):
    """GCM autentica: byte trocado no banco levanta erro em vez de devolver lixo."""
    blob = bytearray(seg.cifrar("key", context="p"))
    blob[-1] ^= 0x01
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(bytes(blob), context="p")


def test_chave_mestra_errada_falha(key, monkeypatch):
    blob = seg.cifrar("key", context="p")
    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(blob, context="p")


def test_sem_chave_mestra_falha_alto(monkeypatch):
    """Sem `APP_SECRET_KEY` não há mode degradado (mesma dureza do `DATABASE_URL`, ADR-020)."""
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    assert not seg.configurada()
    with pytest.raises(seg.ChaveMestraAusente):
        seg.cifrar("x", context="p")


def test_rotacao_aceita_a_chave_antiga(key, monkeypatch):
    """Trocar a `APP_SECRET_KEY` não pode derrubar o que já está gravado: durante a janela, a
    anterior continua decifrando, e `recifrar` migra a linha."""
    antiga = seg.gerar_chave_mestra()
    monkeypatch.setenv(seg.VAR_CHAVE, antiga)
    blob = seg.cifrar("key", context="p")
    kid_antigo = seg.key_id_do_blob(blob)

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.setenv(seg.VAR_CHAVE_ANTIGA, antiga)
    assert seg.decifrar(blob, context="p") == "key"

    novo = seg.recifrar(blob, context="p")
    assert seg.key_id_do_blob(novo) == seg.key_id_atual() != kid_antigo
    monkeypatch.delenv(seg.VAR_CHAVE_ANTIGA)
    assert seg.decifrar(novo, context="p") == "key"   # já não depende da antiga


def test_chave_mestra_aceita_texto_livre(monkeypatch):
    """Operador colando uma password à mão deriva 32 bytes por SHA-256 em vez de ser recusado."""
    monkeypatch.setenv(seg.VAR_CHAVE, "uma password qualquer digitada à mão")
    assert seg.decifrar(seg.cifrar("k", context="p"), context="p") == "k"


def test_gerar_chave_mestra_tem_32_bytes():
    assert len(base64.urlsafe_b64decode(seg.gerar_chave_mestra())) == 32


def test_ultimos4_nao_vaza_segredo_curto():
    assert seg.ultimos4("sk-or-v1-a0bc7b9d") == "7b9d"
    assert seg.ultimos4("curta") == ""


def test_so_o_resolver_decifra():
    """Guarda estrutural: a key em claro existe em UM ponto do código. Se um service, uma
    rota ou um template chamar `decifrar`, o segredo passa a poder subir para o HTTP."""
    import pesquisa_precos

    permitidos = {"pesquisa_precos.providers.resolver", "pesquisa_precos.db.secret"}
    raiz = Path(pesquisa_precos.__file__).parent
    infratores = []
    for m in pkgutil.walk_packages([str(raiz)], prefix="pesquisa_precos."):
        if m.name in permitidos:
            continue
        arquivo = Path(importlib.util.find_spec(m.name).origin or "")
        if arquivo.suffix != ".py":
            continue
        if "segredo.decifrar" in arquivo.read_text(encoding="utf-8"):
            infratores.append(m.name)
    assert not infratores, f"decifram segredo fora do resolver: {infratores}"
