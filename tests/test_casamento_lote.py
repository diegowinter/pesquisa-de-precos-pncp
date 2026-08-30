"""
Casamento em lote da etapa 5 (ADR-024) — uma chamada por documento, não por item.
"""

from pesquisa_precos.core.prompts import montar_prompt_casar_itens_tabela
from pesquisa_precos.providers.llm_curador import Curador
from pesquisa_precos.steps.e5_extract import _lotes_de_itens

TABELA = (
    "| Item | Descrição | Qtd | Unit |\n"
    "| --- | --- | --- | --- |\n"
    "| 1 | Almofada ergonômica para mouse, espuma de poliuretano | 200 | 38,99 |\n"
    "| 2 | Apoio de punho para teclado, gel | 154 | 53,15 |\n"
)

CANDIDATOS = [
    {"item_key": "c::1", "numeroItem": 1, "descricao_api": "Mouse Pad",
     "quantidade": "200", "preco_unitario": "38.99"},
    {"item_key": "c::17", "numeroItem": 17, "descricao_api": "Coturno",
     "quantidade": "186", "preco_unitario": "220.00"},
]


class _Llm:
    """Curador falso: registra quantas vezes foi chamado e o que recebeu."""

    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = 0
        self.ultimo_prompt = ""

    def invoke(self, mensagens):
        self.chamadas += 1
        self.ultimo_prompt = mensagens[0].content
        return type("R", (), {"content": self.resposta})()


def _curador(resposta):
    c = Curador.__new__(Curador)
    c._prompts_ativos = {}
    c.llm = _Llm(resposta)
    return c


class TestPromptDosCandidatos:
    def test_leva_a_tabela_e_todos_os_candidatos_de_uma_vez(self):
        prompt = montar_prompt_casar_itens_tabela(CANDIDATOS, TABELA)
        assert "Almofada ergonômica" in prompt
        assert "Mouse Pad" in prompt and "Coturno" in prompt
        assert "[1]" in prompt and "[17]" in prompt

    def test_avisa_que_a_maioria_nao_estar_e_o_esperado(self):
        """Sem isso o modelo tenta casar tudo, e casamento forçado vira preço errado no
        produto final. É a regra que a ADR-024 identificou como a mais cara de perder."""
        prompt = montar_prompt_casar_itens_tabela(CANDIDATOS, TABELA).lower()
        assert "maioria" in prompt and "não force" in prompt


class TestCasarItensTabela:
    def test_uma_chamada_para_todos_os_candidatos(self):
        c = _curador('{"itens": [{"numero_item": 1, "descricao_completa": "Almofada", '
                     '"preco_unitario": "38,99", "quantidade": "200", "fornecedor": ""}]}')
        c.casar_itens_tabela(CANDIDATOS, TABELA)
        assert c.llm.chamadas == 1, "voltou a perguntar item a item (ver ADR-024)"

    def test_candidato_ausente_da_resposta_nao_vira_entrada(self):
        """"Não está neste documento" é o caso COMUM: um pregão gera N atas e cada uma
        registra o que um fornecedor ganhou. Ausência não pode virar casamento vazio."""
        c = _curador('{"itens": [{"numero_item": 1, "descricao_completa": "Almofada", '
                     '"preco_unitario": "38,99", "quantidade": "200", "fornecedor": ""}]}')
        achados = c.casar_itens_tabela(CANDIDATOS, TABELA)
        assert set(achados) == {1}
        assert 17 not in achados

    def test_lista_vazia_e_resposta_valida(self):
        c = _curador('{"itens": []}')
        assert c.casar_itens_tabela(CANDIDATOS, TABELA) == {}

    def test_erro_de_chamada_levanta_em_vez_de_virar_nao_encontrado(self):
        """A distinção que faltava: falha de rede/modelo e "o item não está aqui" produziam o
        mesmo `nao_encontrado`, e foi assim que a falha em massa passou despercebida."""
        import pytest

        c = _curador("isto não é JSON")
        with pytest.raises(RuntimeError):
            c.casar_itens_tabela(CANDIDATOS, TABELA)

    def test_erro_carrega_a_resposta_crua_do_modelo(self):
        """"Expecting value: line 1 column 1" diz que veio vazio, não POR QUE.

        Em 2026-08-29 um documento falhou com essa mensagem e foi preciso ir ao painel do
        OpenRouter para descobrir o que o modelo tinha respondido. A causa precisa vir junto.
        """
        import pytest

        c = _curador("desculpe, não posso ajudar com isso")
        with pytest.raises(RuntimeError, match="desculpe"):
            c.casar_itens_tabela(CANDIDATOS, TABELA)

    def test_resposta_vazia_e_dita_como_vazia(self):
        import pytest

        c = _curador("")
        with pytest.raises(RuntimeError, match="resposta vazia"):
            c.casar_itens_tabela(CANDIDATOS, TABELA)


class TestLotes:
    def test_compra_pequena_vai_num_lote_so(self):
        assert len(_lotes_de_itens(CANDIDATOS, teto_chars=40_000)) == 1

    def test_compra_grande_e_dividida(self):
        grandes = [{"numeroItem": n, "descricao_api": "x" * 5_000} for n in range(10)]
        lotes = _lotes_de_itens(grandes, teto_chars=12_000)
        assert len(lotes) > 1
        assert sum(len(x) for x in lotes) == 10, "nenhum candidato pode sumir na divisão"

    def test_item_maior_que_o_teto_nao_some(self):
        gigante = [{"numeroItem": 1, "descricao_api": "x" * 90_000}]
        lotes = _lotes_de_itens(gigante, teto_chars=10_000)
        assert sum(len(x) for x in lotes) == 1
