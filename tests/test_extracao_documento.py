"""
Regras de negócio da etapa 5 (`core/extraction.py`) — confirmação do item e veredito.

Herdado de `tests/test_extracao.py`, que morreu junto com o pacote `strategies/` (ADR-023).
O que ficou é justamente o que a troca de abordagem NÃO mudou: `num` e `validar_extracao`
nunca dependeram de como o texto chegou ao processo. Os testes de `janela_para_item` e
`_variantes_preco` saíram com a estratégia de janela.
"""

from pesquisa_precos.core.extraction import (
    BANDA_SANIDADE_MAX,
    BANDA_SANIDADE_MIN,
    num,
    validar_extracao,
)


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
