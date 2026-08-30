"""Custo e evidência: o que a etapa 5 registra além do resultado.

As três regras aqui nasceram do mesmo dia (2026-08-30), em que não deu para responder nem
"esses documentos chegaram ao modelo?" nem "quanto custaram?".
"""
from types import SimpleNamespace

from pesquisa_precos.steps.e5_extract import _custo, _sem_tabela

ITENS = [{"item_key": "k::1", "numeroItem": 1, "descricao_api": "coturno",
          "unidade": "un", "quantidade": "2", "preco_unitario": "10"}]


def test_sem_tabela_carrega_o_hash_quando_o_modelo_viu_o_pdf():
    """`hash` preenchido + `tabela_texto` vazia = o modelo respondeu que não há tabela."""
    _, extracao = _sem_tabela(ITENS, "abc123", (900, 12))
    assert extracao["hash_arquivo"] == "abc123"
    assert extracao["tabela_texto"] == ""
    assert extracao["uso_extract"] == (900, 12)


def test_sem_tabela_sem_hash_quando_nem_baixou():
    """Sem arquivo publicado ou grande demais: a extração não aconteceu, e o hash diz isso."""
    _, extracao = _sem_tabela(ITENS)
    assert extracao["hash_arquivo"] is None
    assert extracao["uso_extract"] == (0, 0)


def test_custo_usa_as_tarifas_do_provedor():
    info = SimpleNamespace(cost_in_per_mtok=3.0, cost_out_per_mtok=15.0,
                           cost_usd_per_call=None)
    assert _custo(info, (1_000_000, 0)) == 3.0
    assert _custo(info, (0, 1_000_000)) == 15.0


def test_custo_zero_quando_o_provedor_nao_tem_tarifa():
    """A GPU caseira não cobra. Zero ali é a verdade, não dado faltando."""
    info = SimpleNamespace(cost_in_per_mtok=None, cost_out_per_mtok=None,
                           cost_usd_per_call=None)
    assert _custo(info, (10_000, 5_000)) == 0.0


def test_custo_por_chamada_soma_ao_de_token():
    info = SimpleNamespace(cost_in_per_mtok=None, cost_out_per_mtok=None,
                           cost_usd_per_call=0.002)
    assert _custo(info, (10, 10)) == 0.002


def test_toda_chamada_ao_modelo_passa_pela_porta_que_mede():
    """`llm_call` ficou vazia por anos porque medir era de quem chamava. Agora é de `_invoke`,
    e a garantia é estrutural: `llm.invoke` só aparece uma vez no arquivo, dentro dela."""
    import pathlib

    fonte = pathlib.Path("pesquisa_precos/providers/llm_curador.py").read_text(
        encoding="utf-8")
    assert fonte.count("self.llm.invoke") == 1
    corpo = fonte[fonte.index("def _invoke"):fonte.index("def _invocar_json")]
    assert "self.llm.invoke" in corpo


# ── O plugin de PDF ──────────────────────────────────────────────────────────────────

def test_auto_nao_declara_plugin():
    """Medido em 2026-08-30, mesma ata e mesmo minuto:

        auto (sem plugin)  →  17.878 tokens de entrada, tabela completa
        cloudflare-ai      →       557 tokens de entrada, SEM_TABELA

    Declarar o engine errado não levanta erro: a etapa "termina bem" devolvendo documento
    vazio. Foi assim que 1.156 de 1.165 extrações produziram nada.
    """
    from pesquisa_precos.steps.e5_extract import Params, _plugin_pdf

    assert _plugin_pdf("auto") == {}
    assert Params().pdf_engine == "auto"


def test_engine_declarado_vai_no_corpo():
    from pesquisa_precos.steps.e5_extract import _plugin_pdf

    assert _plugin_pdf("mistral-ocr") == {
        "plugins": [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}]}


def test_roteamento_de_provedor_pede_o_mais_barato():
    """Sem `provider` no corpo o OpenRouter escolhe, e em 2026-08-30 escolheu 27× mais caro
    (Parasail ~US$ 2,80/Mtok vs Cloudflare ~US$ 0,10/Mtok). Nada falha — só a fatura muda."""
    from pesquisa_precos.steps.e5_extract import Params, _roteamento

    assert Params().provider_sort == "price"
    assert _roteamento("price") == {"provider": {"sort": "price"}}
    assert _roteamento("livre") == {}
