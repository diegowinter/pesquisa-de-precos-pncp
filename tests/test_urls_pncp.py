"""
Reconstrução da URL do PNCP — a rede de segurança do descarte de PDF (ADR-012).

Sem esta função, "não migrar os 111 GB de PDF" vira uma decisão sem volta. Os casos abaixo
usam números de controle REAIS do acervo.
"""

from pesquisa_precos.core.collection.urls import partes_controle, url_documento


class TestPartes:
    def test_contrato(self):
        p = partes_controle("01664910000131-2-000068/2026")
        assert p == {"cnpj": "01664910000131", "tipo": "2", "sequencial": 68,
                     "ano": 2026, "sequencial_ata": None}

    def test_ata(self):
        p = partes_controle("00000368000150-1-000009/2026-000001")
        assert p["sequencial"] == 9 and p["sequencial_ata"] == 1 and p["ano"] == 2026

    def test_formato_desconhecido(self):
        assert partes_controle("lixo") is None
        assert partes_controle("") is None
        assert partes_controle(None) is None


class TestUrl:
    def test_contrato(self):
        assert url_documento("01664910000131-2-000068/2026", "contrato") == \
            "https://pncp.gov.br/app/contratos/01664910000131/2026/68"

    def test_ata(self):
        assert url_documento("00000368000150-1-000009/2026-000001", "ata") == \
            "https://pncp.gov.br/app/atas/00000368000150/2026/9/1"

    def test_zero_a_esquerda_e_removido(self):
        """Os sequenciais vêm zero-padded no número de controle e SEM padding nas rotas do
        PNCP. Preservar o zero daria 404 silencioso — a URL parece certa e não abre."""
        assert "/2026/9/1" in url_documento("00000368000150-1-000009/2026-000001", "ata")

    def test_irreconhecivel_devolve_vazio(self):
        """Não levanta: a migração processa 68 mil documentos e um número malformado não
        pode derrubar o lote — ele é contado e reportado (05_MIGRACAO §4)."""
        assert url_documento("não é um número", "contrato") == ""
