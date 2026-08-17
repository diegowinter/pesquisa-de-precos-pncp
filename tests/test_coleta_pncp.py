"""
Parsers da API do PNCP (Fase 9 — prioridade 1 de docs/08_CONVENCOES.md §6: "o contrato muda
sem aviso"). Cobre as funções puras de `core.coleta.coleta_pncp` e `core.coleta.consultar_itens`
com fixtures de resposta gravadas (formato real do PNCP, sem bater na rede).
"""

from unittest.mock import patch

from pesquisa_precos.core.coleta import coleta_pncp, consultar_itens


class TestMontarItemKey:
    def test_combina_controle_e_numero(self):
        assert coleta_pncp.montar_item_key("123-1-000456/2026", 3) == "123-1-000456/2026::3"


class TestBaseResultado:
    def test_contrato_usa_numero_sequencial_direto(self):
        r = {"numero_sequencial": "456", "orgao_cnpj": "12345678000199",
            "orgao_nome": "Prefeitura X", "unidade_federativa_sigla": "SP",
            "numero_controle_pncp": "123-2-000456/2026", "ano": 2026}
        base = coleta_pncp._base_resultado(r, "contrato")
        assert base["numero_sequencial"] == "456"
        assert base["_seq_ata"] is None
        assert base["uf"] == "SP"

    def test_ata_usa_numero_sequencial_compra_e_guarda_seq_proprio(self):
        r = {"numero_sequencial": "77", "numero_sequencial_compra_ata": "456",
            "orgao_cnpj": "12345678000199", "numero_controle_pncp": "123-1-000456/2026-77"}
        base = coleta_pncp._base_resultado(r, "ata")
        assert base["numero_sequencial"] == "456"
        assert base["_seq_ata"] == "77"

    def test_uf_com_fallback_para_campo_uf(self):
        r = {"uf": "RJ", "numero_sequencial": "1", "orgao_cnpj": "x",
            "numero_controle_pncp": "y"}
        assert coleta_pncp._base_resultado(r, "contrato")["uf"] == "RJ"

    def test_data_prioriza_publicacao_pncp(self):
        r = {"data_publicacao_pncp": "2026-01-05", "data_assinatura": "2026-01-10",
            "numero_sequencial": "1", "orgao_cnpj": "x", "numero_controle_pncp": "y"}
        assert coleta_pncp._base_resultado(r, "contrato")["data"] == "2026-01-05"


class TestColetarDeBaseSemIdentificacao:
    def test_sem_cnpj_ano_ou_sequencial_e_sem_identificacao(self):
        base = {"orgao_cnpj": "", "ano": 2026, "numero_sequencial": "1",
                "numeroControlePNCP": "x", "orgao": "", "uf": "", "data": "",
                "data_fim_vigencia": "", "data_assinatura": ""}
        linhas, status = coleta_pncp._coletar_de_base(base, "contrato", "termo x")
        assert linhas == [] and status == "sem_identificacao"

    def test_ata_sem_seq_ata_e_sem_identificacao(self):
        base = {"orgao_cnpj": "123", "ano": 2026, "numero_sequencial": "1", "_seq_ata": None,
                "numeroControlePNCP": "x", "orgao": "", "uf": "", "data": "",
                "data_fim_vigencia": "", "data_assinatura": ""}
        linhas, status = coleta_pncp._coletar_de_base(base, "ata", "termo x")
        assert linhas == [] and status == "sem_identificacao"


class TestPrecoHomologadoVsEstimado:
    """A regra de negócio nº 1 do domínio: preço = HOMOLOGADO quando há resultado; cai no
    ESTIMADO só quando não há (docs/08_CONVENCOES.md §5.9 — divergência é sinal, não erro)."""

    def _base_valida(self):
        return {"orgao_cnpj": "123", "ano": 2026, "numero_sequencial": "1", "_seq_ata": None,
                "numeroControlePNCP": "ctrl-1", "orgao": "Prefeitura", "uf": "SP", "data": "2026-01-01",
                "data_fim_vigencia": "", "data_assinatura": ""}

    @patch("pesquisa_precos.core.coleta.consultar_arquivos.listar_arquivos")
    @patch("pesquisa_precos.core.coleta.consultar_arquivos.selecionar_do_tipo")
    @patch("pesquisa_precos.core.coleta.consultar_itens.resolver_sequencial_compra_contrato")
    @patch("pesquisa_precos.core.coleta.consultar_itens.fetch_itens")
    @patch("pesquisa_precos.core.coleta.consultar_itens.filtrar_homologados")
    @patch("pesquisa_precos.core.coleta.consultar_itens.fetch_resultado_vencedor")
    def test_usa_homologado_quando_ha_resultado(
        self, mock_venc, mock_filtra, mock_fetch, mock_resolve, mock_sel, mock_listar,
    ):
        mock_listar.return_value = [{}]
        mock_sel.return_value = [{}]
        mock_resolve.return_value = "9"
        item = {"numeroItem": 1, "descricao": "caneta", "unidadeMedida": "UN",
               "quantidade": 10, "valorUnitarioEstimado": 5.0, "temResultado": True}
        mock_fetch.return_value = [item]
        mock_filtra.return_value = [item]
        mock_venc.return_value = {"valorUnitarioHomologado": 4.5,
                                  "nomeRazaoSocialFornecedor": "Fornecedor Y",
                                  "dataResultado": "2026-02-01"}

        linhas, status = coleta_pncp._coletar_de_base(self._base_valida(), "contrato", "termo x")
        assert status == "ok"
        assert linhas[0]["preco_unitario"] == 4.5
        assert linhas[0]["preco_estimado"] == 5.0
        assert linhas[0]["fornecedor"] == "Fornecedor Y"

    @patch("pesquisa_precos.core.coleta.consultar_arquivos.listar_arquivos")
    @patch("pesquisa_precos.core.coleta.consultar_arquivos.selecionar_do_tipo")
    @patch("pesquisa_precos.core.coleta.consultar_itens.resolver_sequencial_compra_contrato")
    @patch("pesquisa_precos.core.coleta.consultar_itens.fetch_itens")
    @patch("pesquisa_precos.core.coleta.consultar_itens.filtrar_homologados")
    def test_cai_no_estimado_sem_resultado(
        self, mock_filtra, mock_fetch, mock_resolve, mock_sel, mock_listar,
    ):
        mock_listar.return_value = [{}]
        mock_sel.return_value = [{}]
        mock_resolve.return_value = "9"
        item = {"numeroItem": 1, "descricao": "caneta", "unidadeMedida": "UN",
               "quantidade": 10, "valorUnitarioEstimado": 5.0, "temResultado": False}
        mock_fetch.return_value = [item]
        mock_filtra.return_value = [item]

        linhas, status = coleta_pncp._coletar_de_base(self._base_valida(), "contrato", "termo x")
        assert status == "ok"
        assert linhas[0]["preco_unitario"] == 5.0
        assert linhas[0]["fornecedor"] == ""


class TestFiltrarHomologados:
    def test_mantem_so_situacao_homologado(self):
        itens = [{"situacaoCompraItemNome": "Homologado"}, {"situacaoCompraItemNome": "Cancelado"},
                 {"situacaoCompraItemNome": "  Homologado  "}]  # .strip() aceita espaços em volta
        out = consultar_itens.filtrar_homologados(itens)
        assert len(out) == 2
        assert all(i["situacaoCompraItemNome"].strip() == "Homologado" for i in out)

    def test_lista_vazia(self):
        assert consultar_itens.filtrar_homologados([]) == []


class TestResolverSequencialCompraContrato:
    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_extrai_sequencial_da_compra(self, mock_get):
        mock_get.return_value = {"numeroControlePncpCompra": "123-1-000547/2026"}
        seq = consultar_itens.resolver_sequencial_compra_contrato("123", 2026, "9")
        assert seq == "547"

    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_sem_controle_de_compra_devolve_none(self, mock_get):
        mock_get.return_value = {}
        assert consultar_itens.resolver_sequencial_compra_contrato("123", 2026, "9") is None

    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_formato_inesperado_devolve_none(self, mock_get):
        mock_get.return_value = {"numeroControlePncpCompra": "formato-totalmente-diferente"}
        assert consultar_itens.resolver_sequencial_compra_contrato("123", 2026, "9") is None


class TestFetchResultadoVencedor:
    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_prioriza_valido_com_homologado_e_menor_ordem(self, mock_get):
        mock_get.return_value = [
            {"ordemClassificacaoSrp": 2, "valorUnitarioHomologado": 10.0},
            {"ordemClassificacaoSrp": 1, "valorUnitarioHomologado": None},  # 1º colocado, sem valor
            {"ordemClassificacaoSrp": 3, "valorUnitarioHomologado": 12.0},
        ]
        vencedor = consultar_itens.fetch_resultado_vencedor("123", 2026, "9", 1)
        # entre os VÁLIDOS (com valorUnitarioHomologado), o de menor ordem vence
        assert vencedor["ordemClassificacaoSrp"] == 2

    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_sem_nenhum_valido_cai_no_menor_ordem_bruto(self, mock_get):
        mock_get.return_value = [
            {"ordemClassificacaoSrp": 2, "valorUnitarioHomologado": None},
            {"ordemClassificacaoSrp": 1, "valorUnitarioHomologado": None},
        ]
        vencedor = consultar_itens.fetch_resultado_vencedor("123", 2026, "9", 1)
        assert vencedor["ordemClassificacaoSrp"] == 1

    @patch("pesquisa_precos.core.coleta.consultar_itens._get")
    def test_lista_vazia_devolve_none(self, mock_get):
        mock_get.return_value = []
        assert consultar_itens.fetch_resultado_vencedor("123", 2026, "9", 1) is None
