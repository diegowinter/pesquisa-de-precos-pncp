"""
Guarda da Fase 7 + Fase 14: resolução de provedor por capacidade, a proibição de fallback em
`embed` (ADR-006 §2) e o contrato dos `Protocol`.

Tudo aqui é OFFLINE — sem banco, sem rede, sem GPU: uma "sessão" fake basta para exercitar a
resolução sem um Postgres de verdade.

Antes da ADR-022 metade deste arquivo testava a resolução pelo `.env`. Esse caminho **saiu**, e
os testes que o cobriam viraram o inverso: sem provedor apontado, resolver LEVANTA. É a mesma
troca que a ADR-020 fez com `--fonte csv` — o que era fallback virou erro.
"""

import pytest

from pesquisa_precos.providers.protocolos import InfoProvedor, ProvedorChat, ProvedorEmbed
from pesquisa_precos.providers.resolver import (
    CapacidadeNaoConfigurada,
    FallbackProibidoEmbedError,
    Provedores,
    resolver_capacidade,
)


def _linha(capacidade: str, **extra) -> dict:
    """Uma linha de `capacidade_provedor` JOIN `provedor`, como o repo a devolve."""
    base = {"capacidade": capacidade, "provedor": "openrouter", "modelo": None,
            "fallback": None, "base_url": "https://openrouter.ai/api/v1",
            "api_key_cifrada": None, "modelo_padrao": None, "batch_size": 32,
            "rpm_limite": None, "custo_in_por_mtok": None, "custo_out_por_mtok": None,
            "custo_usd_chamada": None, "permite_fallback": False, "ativo": True}
    return {**base, **extra}






class _SessaoFake:
    """Simula só o suficiente de `Session` p/ `db.repos.execucao.capacidade_provedor_info`:
    o repo faz `sessao.execute(text(...), params).mappings().first()` — aqui devolvemos direto
    a linha (ou `None`), sem interpretar o SQL, então o teste não depende de Postgres."""

    def __init__(self, linhas: dict[str, dict | None]):
        self._linhas = linhas

    def execute(self, _stmt, params):
        return _Resultado(self._linhas.get(params["c"]))


class _Resultado:
    def __init__(self, linha):
        self._linha = linha

    def mappings(self):
        return self

    def first(self):
        return self._linha


# ── sem provedor apontado, resolver LEVANTA (ADR-022) ───────────────────────────────

@pytest.mark.parametrize("capacidade", ["chat", "embed", "rerank", "pdf", "pareamento"])
def test_capacidade_sem_provedor_levanta(capacidade):
    """O caminho `.env` saiu. Não existe mais "cai para o default" — que era exatamente o modo
    em que um erro de configuração virava a etapa rodando com o modelo errado."""
    sessao = _SessaoFake({capacidade: None})
    with pytest.raises(CapacidadeNaoConfigurada) as exc:
        resolver_capacidade(capacidade, sessao=sessao)
    assert "/provedores" in str(exc.value)


def test_mensagem_diz_o_que_fazer():
    """Erro de configuração tem de apontar a saída: a tela, não o arquivo que saiu de cena."""
    sessao = _SessaoFake({"chat": None})
    with pytest.raises(CapacidadeNaoConfigurada) as exc:
        resolver_capacidade("chat", sessao=sessao)
    assert "chat" in str(exc.value) and "/provedores" in str(exc.value)


def test_ocr_nao_e_mais_capacidade_deste_processo():
    """Quem chama o OCR é o serviço de `pdf`, na máquina dele (ADR-021). Resolver `ocr` aqui
    significaria que alguém voltou a rasterizar página neste processo."""
    with pytest.raises(ValueError, match="capacidade desconhecida"):
        resolver_capacidade("ocr", sessao=_SessaoFake({}))


def test_resolver_capacidade_desconhecida_leva_a_erro_claro():
    with pytest.raises(ValueError, match="capacidade desconhecida"):
        resolver_capacidade("visao", sessao=_SessaoFake({}))


# ── resolução via banco (ADR-014: banco manda quando configurado) ───────────────────

def test_resolver_le_o_provedor_do_banco():
    sessao = _SessaoFake({"chat": _linha("chat", modelo="modelo-do-banco",
                                         custo_in_por_mtok=0.5, custo_out_por_mtok=1.5,
                                         custo_usd_chamada=0.0001)})
    r = resolver_capacidade("chat", sessao=sessao)
    assert r.origem == "banco"
    assert r.info.nome == "openrouter"
    assert r.info.modelo == "modelo-do-banco"
    assert r.info.custo_usd_chamada == 0.0001


def test_modelo_da_capacidade_vence_o_padrao_do_provedor():
    """`capacidade_provedor.modelo` é o override por capacidade; `provedor.modelo_padrao` é o
    default. Um provedor pode atender `chat` com modelos diferentes em instalações diferentes."""
    sessao = _SessaoFake({"chat": _linha("chat", modelo="especifico", modelo_padrao="padrao")})
    assert resolver_capacidade("chat", sessao=sessao).info.modelo == "especifico"
    sessao = _SessaoFake({"chat": _linha("chat", modelo=None, modelo_padrao="padrao")})
    assert resolver_capacidade("chat", sessao=sessao).info.modelo == "padrao"


def test_chave_cifrada_e_decifrada_na_resolucao(monkeypatch):
    """O resolver é o único ponto que devolve segredo em claro — e tem de devolvê-lo certo."""
    from pesquisa_precos.db import segredo as seg

    monkeypatch.setenv(seg.VAR_CHAVE, seg.gerar_chave_mestra())
    blob = seg.cifrar("sk-or-v1-do-banco", contexto="openrouter")
    sessao = _SessaoFake({"chat": _linha("chat", api_key_cifrada=blob)})
    assert resolver_capacidade("chat", sessao=sessao).api_key == "sk-or-v1-do-banco"


def test_provedor_sem_chave_resolve_com_string_vazia():
    """Serviço local sem autenticação é caso legítimo — não pode explodir por falta de chave."""
    sessao = _SessaoFake({"pareamento": _linha("pareamento", base_url="http://x:8300")})
    assert resolver_capacidade("pareamento", sessao=sessao).api_key == ""


def test_fallback_proibido_em_embed_levanta_erro_claro():
    """ADR-006 §2: fallback proibido em `embed`. Mesmo que a linha do banco tenha sido editada
    direto (bypassando `db.repos.execucao.apontar_capacidade`, que já recusa isso na escrita),
    a resolução recusa de novo na leitura — duas travas, não uma."""
    sessao = _SessaoFake({"embed": _linha("embed", provedor="gpu_caseira", modelo="bge-m3",
                                          fallback="local",
                                          base_url="http://gpu.local:8100")})
    with pytest.raises(FallbackProibidoEmbedError):
        resolver_capacidade("embed", sessao=sessao)


def test_fallback_e_permitido_em_rerank():
    sessao = _SessaoFake({"rerank": _linha(
        "rerank", provedor="gpu_caseira", modelo="bge-reranker", fallback="local",
        base_url="http://gpu.local:8100", batch_size=16, permite_fallback=True)})
    r = resolver_capacidade("rerank", sessao=sessao)
    assert r.info.fallback_provedor == "local"


# ── Provedores (ctx.provedores): lazy + cacheado ─────────────────────────────────────

def test_provedores_resolucao_nao_instancia_adapter():
    """`.resolucao()` só lê — não deve tentar montar um cliente HTTP (que chamaria rede)."""
    p = Provedores({}, _SessaoFake({"chat": _linha("chat")}))
    r = p.resolucao("chat")
    assert isinstance(r.info, InfoProvedor)


def test_provedores_novo_chat_nao_e_cacheado(monkeypatch):
    """Etapa 3 precisa de um Curador POR THREAD — `.novo_chat()` tem que devolver uma instância
    nova a cada chamada, nunca a mesma (diferente de `.chat`, que É cacheado)."""
    monkeypatch.setattr(
        "pesquisa_precos.providers.llm_curador.Curador.__init__",
        lambda self, **kw: None)
    p = Provedores({}, _SessaoFake({"chat": _linha("chat")}))
    a = p.novo_chat()
    b = p.novo_chat()
    assert a is not b


def test_protocolos_sao_runtime_checkable():
    """As quatro capacidades declaram `Protocol` verificável — é o que permite `ctx.provedores.
    chat`/etc. ser QUALQUER adapter, sem a etapa importar uma classe concreta."""
    assert hasattr(ProvedorChat, "__protocol_attrs__") or ProvedorChat._is_protocol
    assert ProvedorEmbed._is_protocol
