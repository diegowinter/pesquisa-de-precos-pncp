"""
Extração de item a partir de texto (Fase 9 — prioridade 2 de docs/08_CONVENCOES.md §6):
`janela_para_item`, `validar_extracao`, `_variantes_preco`, `num`. Funções puras — teste de
tabela com casos reais, incluindo números BR malformados (`107.222,00` não pode virar `107,22`).
"""

from pesquisa_precos.strategies.base import (
    BANDA_SANIDADE_MAX,
    BANDA_SANIDADE_MIN,
    num,
    validar_extracao,
)
from pesquisa_precos.strategies.window import _variantes_preco, janela_para_item


class TestNum:
    def test_formato_br_ponto_milhar_virgula_decimal(self):
        assert num("107.222,00") == 107222.00

    def test_nao_confunde_milhar_com_decimal(self):
        """O bug clássico: tratar o ponto de milhar como separador decimal faria
        '107.222,00' virar 107,22 — 1000x menor. Não pode acontecer."""
        assert num("107.222,00") != 107.22

    def test_formato_us_ponto_decimal(self):
        assert num("1234.56") == 1234.56

    def test_inteiro_sem_separador(self):
        assert num("500") == 500.0

    def test_valor_com_milhar_grande(self):
        assert num("1.234.567,89") == 1234567.89

    def test_none_e_vazio(self):
        assert num(None) is None
        assert num("") is None
        assert num("   ") is None

    def test_texto_invalido(self):
        assert num("não é número") is None


class TestVariantesPreco:
    def test_gera_formato_br_ponto_milhar(self):
        variantes = _variantes_preco(578538.24)
        assert "578.538,24" in variantes

    def test_gera_formato_br_sem_ponto_milhar(self):
        variantes = _variantes_preco(578538.24)
        assert "578538,24" in variantes

    def test_valor_zero_ou_negativo_sem_variante(self):
        assert _variantes_preco(0) == []
        assert _variantes_preco(-10) == []

    def test_valor_invalido_sem_variante(self):
        assert _variantes_preco("abc") == []

    def test_valor_string_br_aceito(self):
        assert "1.500,00" in _variantes_preco("1500,00")


class TestJanelaParaItem:
    def test_ancora_na_descricao(self):
        texto = "x" * 5000 + "PISTOLA 9MM CALIBRE ESPECIAL" + "y" * 5000
        item = {"descricao_api": "PISTOLA 9MM CALIBRE ESPECIAL", "numeroItem": 1}
        janela = janela_para_item(texto, item, janela_max=9000, raio_desc=200)
        assert "PISTOLA 9MM CALIBRE ESPECIAL" in janela

    def test_ancora_no_preco_quando_descricao_nao_bate(self):
        texto = "a" * 3000 + "valor unitario: 1.500,00 reais" + "b" * 3000
        item = {"descricao_api": "algo que não está no texto", "preco_unitario": 1500.00}
        janela = janela_para_item(texto, item, janela_max=9000, raio_preco=200)
        assert "1.500,00" in janela

    def test_sem_ancora_nenhuma_devolve_prefixo(self):
        texto = "z" * 20000
        item = {"descricao_api": "não existe", "preco_unitario": None}
        janela = janela_para_item(texto, item)
        assert len(janela) <= janela.count("z") + 1  # não estoura o teto de prefixo
        assert janela == texto[: 3000 * 4]

    def test_respeita_teto_janela_max(self):
        texto = "PISTOLA" + "x" * 50000 + "1.500,00" * 3
        item = {"descricao_api": "PISTOLA", "preco_unitario": 1500.00}
        janela = janela_para_item(texto, item, janela_max=1000)
        assert len(janela) <= 1000 + len("\n[...]\n") * 3  # margem para os separadores


class TestValidarExtracao:
    """`status` bate 1:1 com `status_enriquecimento` (docs/02_SCHEMA.md §2)."""

    def test_nao_encontrado_quando_extracao_vazia(self):
        status, preco, div = validar_extracao({"encontrado": False}, {"preco_unitario": 100})
        assert status == "nao_encontrado"
        assert preco is None and div is None

    def test_confirmado_por_quantidade_preco_igual(self):
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 10, "preco_unitario": 250.50}
        item = {"quantidade": 10, "preco_unitario": 250.50}
        status, preco, div = validar_extracao(extraido, item)
        assert status == "pdf_ok"
        assert preco == 250.50
        assert div == 0.0

    def test_confirmado_por_quantidade_preco_diverge(self):
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 10, "preco_unitario": 300.00}
        item = {"quantidade": 10, "preco_unitario": 250.00}
        status, preco, div = validar_extracao(extraido, item)
        assert status == "pdf_ok_diverge"
        assert preco == 300.00
        assert div == 0.2

    def test_preco_fora_da_banda_de_sanidade_e_suspeito(self):
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 5, "preco_unitario": 10000.00}
        item = {"quantidade": 5, "preco_unitario": 250.00}  # 40x o estimado, fora de [0.3,3.0]
        status, preco, _div = validar_extracao(extraido, item)
        assert status == "pdf_ok_preco_suspeito"
        assert preco == 10000.00

    def test_banda_de_sanidade_limites(self):
        assert BANDA_SANIDADE_MIN == 0.3
        assert BANDA_SANIDADE_MAX == 3.0

    def test_confirmado_por_preco_exato_alto_sem_quantidade_batendo(self):
        """Contratos de serviço (qtd=1, doc não a reafirma): match exato de preço alto
        confirma sozinho, acima de PRECO_FINGERPRINT."""
        extraido = {"encontrado": True, "descricao_completa": "servico x",
                   "quantidade": None, "preco_unitario": 1500.00}
        item = {"quantidade": 1, "preco_unitario": 1500.00}
        status, preco, _div = validar_extracao(extraido, item)
        assert status == "pdf_ok"
        assert preco == 1500.00

    def test_qtd_nao_confere_quando_nada_bate(self):
        extraido = {"encontrado": True, "descricao_completa": "item errado",
                   "quantidade": 999, "preco_unitario": 1.00}
        item = {"quantidade": 5, "preco_unitario": 500.00}
        status, preco, div = validar_extracao(extraido, item)
        assert status == "qtd_nao_confere"
        assert preco is None and div is None

    def test_confirmado_sem_preco_legivel(self):
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 10, "preco_unitario": None}
        item = {"quantidade": 10, "preco_unitario": 100.00}
        status, preco, _div = validar_extracao(extraido, item)
        assert status == "pdf_ok_sem_preco"
        assert preco is None

    def test_confirmado_sem_preco_de_referencia_na_api(self):
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 10, "preco_unitario": 50.00}
        item = {"quantidade": 10, "preco_unitario": 0}
        status, preco, _div = validar_extracao(extraido, item)
        assert status == "pdf_ok_sem_ref"
        assert preco == 50.00

    def test_numero_br_malformado_nao_vira_ordem_de_grandeza_errada(self):
        """'107.222,00' não pode ser lido como 107,22 — regressão do bug de milhar/decimal."""
        extraido = {"encontrado": True, "descricao_completa": "item x",
                   "quantidade": 3, "preco_unitario": "107.222,00"}
        item = {"quantidade": 3, "preco_unitario": 107222.00}
        status, preco, _div = validar_extracao(extraido, item)
        assert status == "pdf_ok"
        assert preco == 107222.00
