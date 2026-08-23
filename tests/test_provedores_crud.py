"""
CRUD de provedores (Fase 14 bloco 2, ADR-022). Mesmo padrão de
`tests/test_notification_recipients.py`: pulados sem Postgres.

O que estes testes protegem, além do CRUD em si, é a promessa que permitiu a key sair do
`.env`: ela entra, é cifrada, e **não volta** — nem pelo service, nem pelo HTML, nem pela lista.
"""

import pytest
from sqlalchemy import text

from pesquisa_precos.db import secret as seg
from pesquisa_precos.db import session as db
from pesquisa_precos.services import providers as service
from pesquisa_precos.services.providers import (
    FallbackProibido,
    ProvedorInexistente,
    InvalidProvider,
)

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

PREFIXO = "teste-f14-"


def _limpar():
    with db.session() as sessao:
        sessao.execute(text("DELETE FROM provider_capability WHERE provider LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provider_status WHERE provider LIKE :p"),
                       {"p": f"{PREFIXO}%"})
        sessao.execute(text("DELETE FROM provider WHERE name LIKE :p"), {"p": f"{PREFIXO}%"})


def _snapshot_capabilities():
    """Fotografa `provider_capability` inteira.

    Os testes apontam capabilities REAIS (`chat`, `embed`, ...) para provedores fictícios — não
    há capability "de teste", o enum é fechado. Sem restaurar depois, rodar `pytest` apagaria a
    configuração de produção do operador: as etapas parariam de resolver provider e a culpa
    pareceria do código. Já aconteceu uma vez.
    """
    with db.session() as sessao:
        return [dict(r) for r in sessao.execute(text(
            "SELECT capability, provider, model, fallback FROM provider_capability"
        )).mappings()]


def _restaurar_capabilities(linhas):
    from pesquisa_precos.db.repos import execution as repo

    with db.session() as sessao:
        sessao.execute(text("DELETE FROM provider_capability"))
        for linha in linhas:
            repo.apontar_capacidade(sessao, linha["capability"], linha["provider"],
                                    linha["model"], linha["fallback"])


@pytest.fixture(autouse=True)
def _provedores_de_teste_limpos(monkeypatch):
    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    capabilities = _snapshot_capabilities()
    _limpar()
    yield
    _limpar()
    _restaurar_capabilities(capabilities)


# ── validação (não chega ao banco) ───────────────────────────────────────────────────

@pytest.mark.parametrize("name,base_url,caps", [
    ("", "http://x", ["chat"]),
    (f"{PREFIXO}a", "", ["chat"]),
    (f"{PREFIXO}a", "http://x", []),
    (f"{PREFIXO}a", "http://x", ["capability-que-nao-existe"]),
])
def test_formulario_incompleto_recusado(name, base_url, caps):
    with pytest.raises(InvalidProvider):
        service.salvar(name, caps, base_url)


def test_base_url_vazia_nao_significa_roda_aqui():
    """ADR-021: `base_url` vazio é erro de configuração, não caminho em processo."""
    with pytest.raises(InvalidProvider, match="base_url"):
        service.salvar(f"{PREFIXO}gpu", ["embed"], "   ")


# ── CRUD ─────────────────────────────────────────────────────────────────────────────

def test_criar_e_editar():
    service.salvar(f"{PREFIXO}or", ["chat"], "https://openrouter.ai/api/v1",
                   default_model="model-barato", cost_in_per_mtok=0.01)
    p = service.obter(f"{PREFIXO}or")
    assert p["base_url"] == "https://openrouter.ai/api/v1"
    assert p["default_model"] == "model-barato"
    assert float(p["cost_in_per_mtok"]) == 0.01

    service.salvar(f"{PREFIXO}or", ["chat"], "https://novo.example/v1")
    assert service.obter(f"{PREFIXO}or")["base_url"] == "https://novo.example/v1"


def test_definir_ativo_preserva_os_demais_campos():
    """Desativar não pode ser um upsert que zera batch_size/model pelo caminho."""
    service.salvar(f"{PREFIXO}gpu", ["embed", "rerank"], "http://gpu:8100",
                   default_model="bge-m3", batch_size=64)
    service.definir_ativo(f"{PREFIXO}gpu", False)
    p = service.obter(f"{PREFIXO}gpu")
    assert p["active"] is False
    assert p["default_model"] == "bge-m3" and p["batch_size"] == 64


def test_apontar_capacidade():
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x")
    service.apontar("chat", f"{PREFIXO}or", model="model-x")
    with db.session() as sessao:
        from pesquisa_precos.db.repos import execution as repo
        assert repo.capacidade_provedor_info(sessao, "chat")["provider"] == f"{PREFIXO}or"


def test_apontar_provedor_inexistente():
    with pytest.raises(ProvedorInexistente):
        service.apontar("chat", f"{PREFIXO}nao-existe")


def test_fallback_proibido_em_embed():
    """ADR-006: trocar de provider de embedding no meio mistura espaços vetoriais."""
    service.salvar(f"{PREFIXO}a", ["embed"], "http://a")
    service.salvar(f"{PREFIXO}b", ["embed"], "http://b")
    with pytest.raises(FallbackProibido):
        service.apontar("embed", f"{PREFIXO}a", fallback=f"{PREFIXO}b")
    service.apontar("rerank", f"{PREFIXO}a", fallback=f"{PREFIXO}b")   # permitido fora de embed


# ── a key entra e não volta ────────────────────────────────────────────────────────

def test_chave_e_cifrada_e_nao_volta_pela_listagem():
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta")
    p = service.obter(f"{PREFIXO}or")
    assert p["has_api_key"] is True
    assert p["api_key_last4"] == "reta"
    # nenhum campo da listagem carrega o segredo
    assert "supersecreta" not in str(p)
    assert "api_key_encrypted" not in p


def test_chave_no_banco_nao_e_legivel():
    """O ponto inteiro da ADR-022: `pg_dump` do bytea não pode conter a key em claro."""
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta")
    with db.session() as sessao:
        blob = bytes(sessao.execute(
            text("SELECT api_key_encrypted FROM provider WHERE name = :n"),
            {"n": f"{PREFIXO}or"}).scalar_one())
    assert b"supersecreta" not in blob and b"sk-or" not in blob


def test_resolver_decifra_a_chave_gravada():
    """A ponta a ponta que importa: o adapter tem de receber a key certa, em claro."""
    from pesquisa_precos.providers.resolver import resolver_capacidade

    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta",
                   default_model="model-x")
    service.apontar("chat", f"{PREFIXO}or")
    with db.session() as sessao:
        r = resolver_capacidade("chat", sessao=sessao)
    assert r.source == "banco"
    assert r.api_key == "sk-or-v1-supersecreta"


def test_salvar_sem_chave_nao_apaga_a_existente():
    """O campo do formulário nasce vazio a cada edição — se branco apagasse, editar a
    `base_url` destruiria a key."""
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta")
    service.salvar(f"{PREFIXO}or", ["chat"], "http://y")
    assert service.obter(f"{PREFIXO}or")["has_api_key"] is True


def test_limpar_chave():
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta")
    service.limpar_api_key(f"{PREFIXO}or")
    p = service.obter(f"{PREFIXO}or")
    assert p["has_api_key"] is False and p["api_key_last4"] is None


def test_gravar_chave_em_provedor_inexistente():
    with pytest.raises(ProvedorInexistente):
        service.gravar_api_key(f"{PREFIXO}nao-existe", "sk-x")


def test_sem_chave_mestra_nao_grava_provedor_pela_metade(monkeypatch):
    """Falhar ANTES do INSERT: gravar o provider e perder a key em silêncio é o pior caso."""
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    with pytest.raises(seg.ChaveMestraAusente):
        service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-x")
    assert service.obter(f"{PREFIXO}or") is None


# ── rotação da key-mestra ──────────────────────────────────────────────────────────

def _a_recifrar_do_teste():
    """Só as linhas deste teste. Numa instalação real há provedores de verdade no banco, e
    trocar a key-mestra aqui faz TODOS eles aparecerem como pendentes — o que é correto,
    mas não é o que este teste mede."""
    return [n for n in service.keys_a_recifrar() if n.startswith(PREFIXO)]


def test_rotacao_lista_e_recifra(monkeypatch):
    antiga = seg.gerar_chave_mestra()
    monkeypatch.setenv(seg.VAR_CHAVE, antiga)
    service.salvar(f"{PREFIXO}or", ["chat"], "http://x", api_key="sk-or-v1-supersecreta")
    assert _a_recifrar_do_teste() == []

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.setenv(seg.VAR_CHAVE_ANTIGA, antiga)
    assert f"{PREFIXO}or" in _a_recifrar_do_teste()

    # `recifrar_tudo` varre o banco inteiro. Numa instalação real, as linhas de verdade estão
    # cifradas por uma key-mestra que este teste não tem — elas caem em `falharam`, e é
    # justamente isso que se quer: uma linha ilegível não pode abortar a rotação das outras.
    resultado = service.recifrar_tudo()
    assert resultado["recifradas"] >= 1
    assert _a_recifrar_do_teste() == []

    # e a key continua correta depois de re-cifrada, já sem a antiga no ambiente
    monkeypatch.delenv(seg.VAR_CHAVE_ANTIGA)
    service.apontar("chat", f"{PREFIXO}or")
    from pesquisa_precos.providers.resolver import resolver_capacidade
    with db.session() as sessao:
        assert resolver_capacidade("chat", sessao=sessao).api_key == "sk-or-v1-supersecreta"


def test_diagnostico_chave_mestra(monkeypatch):
    assert service.diagnostico_chave_mestra()["configurada"] is True
    monkeypatch.delenv(seg.VAR_CHAVE, raising=False)
    diag = service.diagnostico_chave_mestra()
    assert diag["configurada"] is False and diag["key_id"] is None


def test_linha_ilegivel_nao_aborta_a_rotacao(monkeypatch):
    """Duas rotações sem re-cifrar no meio (ou um restore de dump antigo) deixam linhas que não
    decifram com nenhuma key disponível. Elas têm de ser REPORTADAS, não derrubar a rotação
    das demais — senão um problema de uma linha vira um problema de todas."""
    perdida = seg.gerar_chave_mestra()
    monkeypatch.setenv(seg.VAR_CHAVE, perdida)
    service.salvar(f"{PREFIXO}perdida", ["chat"], "http://x", api_key="sk-inalcancavel")

    antiga = seg.gerar_chave_mestra()
    monkeypatch.setenv(seg.VAR_CHAVE, antiga)
    service.salvar(f"{PREFIXO}ok", ["chat"], "http://y", api_key="sk-recuperavel")

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    monkeypatch.setenv(seg.VAR_CHAVE_ANTIGA, antiga)   # só a segunda é recuperável

    resultado = service.recifrar_tudo()
    assert f"{PREFIXO}perdida" in resultado["falharam"]
    assert resultado["recifradas"] >= 1
    assert f"{PREFIXO}ok" not in _a_recifrar_do_teste()
