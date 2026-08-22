"""
Cifra dos segredos no banco (Fase 14, ADR-022) — `db/segredo.py`.

Sem banco: tudo aqui é in-process. O que estes testes protegem é a razão de a chave poder sair
do `.env`: se qualquer uma destas propriedades cair, guardar a chave no banco vira risco em vez
de conveniência.
"""

import base64
import importlib
import pkgutil
from pathlib import Path

import pytest

from pesquisa_precos.db import segredo as seg


@pytest.fixture
def chave(monkeypatch):
    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.delenv(seg.VAR_CHAVE_ANTIGA, raising=False)


def test_roundtrip(chave):
    blob = seg.cifrar("sk-or-v1-segredo", contexto="openrouter")
    assert seg.decifrar(blob, contexto="openrouter") == "sk-or-v1-segredo"


def test_criptograma_nao_contem_o_segredo_em_claro(chave):
    """O motivo de existir: um `pg_dump` do `bytea` não pode conter a chave legível."""
    blob = seg.cifrar("sk-or-v1-segredo", contexto="openrouter")
    assert b"sk-or" not in blob and b"segredo" not in blob


def test_nonce_novo_a_cada_cifra(chave):
    """Reusar nonce em GCM quebra a cifra inteira — dois criptogramas do mesmo texto têm de
    diferir."""
    a = seg.cifrar("mesma-chave", contexto="p")
    b = seg.cifrar("mesma-chave", contexto="p")
    assert a != b
    assert seg.decifrar(a, contexto="p") == seg.decifrar(b, contexto="p")


def test_aad_amarra_o_blob_ao_provedor(chave):
    """Copiar `api_key_cifrada` do provedor A para o B tem de FALHAR, não fazer o B usar a
    chave do A em silêncio."""
    blob = seg.cifrar("chave-do-a", contexto="provedor_a")
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(blob, contexto="provedor_b")


def test_blob_adulterado_falha(chave):
    """GCM autentica: byte trocado no banco levanta erro em vez de devolver lixo."""
    blob = bytearray(seg.cifrar("chave", contexto="p"))
    blob[-1] ^= 0x01
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(bytes(blob), contexto="p")


def test_chave_mestra_errada_falha(chave, monkeypatch):
    blob = seg.cifrar("chave", contexto="p")
    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    with pytest.raises(seg.SegredoInvalido):
        seg.decifrar(blob, contexto="p")


def test_sem_chave_mestra_falha_alto(monkeypatch):
    """Sem `APP_SECRET_KEY` não há modo degradado (mesma dureza do `DATABASE_URL`, ADR-020)."""
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    assert not seg.configurada()
    with pytest.raises(seg.ChaveMestraAusente):
        seg.cifrar("x", contexto="p")


def test_rotacao_aceita_a_chave_antiga(chave, monkeypatch):
    """Trocar a `APP_SECRET_KEY` não pode derrubar o que já está gravado: durante a janela, a
    anterior continua decifrando, e `recifrar` migra a linha."""
    antiga = seg.gerar_chave_mestra()
    monkeypatch.setenv(seg.VAR_CHAVE, antiga)
    blob = seg.cifrar("chave", contexto="p")
    kid_antigo = seg.key_id_do_blob(blob)

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.setenv(seg.VAR_CHAVE_ANTIGA, antiga)
    assert seg.decifrar(blob, contexto="p") == "chave"

    novo = seg.recifrar(blob, contexto="p")
    assert seg.key_id_do_blob(novo) == seg.key_id_atual() != kid_antigo
    monkeypatch.delenv(seg.VAR_CHAVE_ANTIGA)
    assert seg.decifrar(novo, contexto="p") == "chave"   # já não depende da antiga


def test_chave_mestra_aceita_texto_livre(monkeypatch):
    """Operador colando uma senha à mão deriva 32 bytes por SHA-256 em vez de ser recusado."""
    monkeypatch.setenv(seg.VAR_CHAVE, "uma senha qualquer digitada à mão")
    assert seg.decifrar(seg.cifrar("k", contexto="p"), contexto="p") == "k"


def test_gerar_chave_mestra_tem_32_bytes():
    assert len(base64.urlsafe_b64decode(seg.gerar_chave_mestra())) == 32


def test_ultimos4_nao_vaza_segredo_curto():
    assert seg.ultimos4("sk-or-v1-a0bc7b9d") == "7b9d"
    assert seg.ultimos4("curta") == ""


def test_so_o_resolver_decifra():
    """Guarda estrutural: a chave em claro existe em UM ponto do código. Se um service, uma
    rota ou um template chamar `decifrar`, o segredo passa a poder subir para o HTTP."""
    import pesquisa_precos

    permitidos = {"pesquisa_precos.providers.resolver", "pesquisa_precos.db.segredo"}
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
