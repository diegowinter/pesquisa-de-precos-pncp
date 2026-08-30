"""A distinção entre "este arquivo não dá" e "o serviço está fora".

Tratar as duas igual produzia dois defeitos ao mesmo tempo (2026-08-30): o documento recusado
pelo parser voltava à fila para tomar o mesmo 400, e o circuit breaker abortava a etapa
inteira culpando o provedor.
"""
import pytest

from pesquisa_precos.core.extraction import falha_permanente
from pesquisa_precos.steps.e5_extract import LIMITE_FALHA_TOTAL, _Acumulador, _FalhaTotal


class _ComStatus(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.mark.parametrize("status", [400, 413, 415, 422])
def test_4xx_de_conteudo_e_veredito_sobre_o_arquivo(status):
    assert falha_permanente(_ComStatus(status))


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_credencial_limite_e_5xx_sao_do_servico(status):
    """Estes DEVEM alimentar o circuit breaker: chave errada aborta a etapa, não o documento."""
    assert falha_permanente(_ComStatus(status)) is None


def test_texto_do_parser_sem_status_legivel():
    exc = Exception("Failed to parse a.pdf: The file exceeds the maximum size supported")
    assert falha_permanente(exc)


def test_timeout_e_conexao_valem_retentativa():
    assert falha_permanente(TimeoutError("timed out")) is None
    assert falha_permanente(ConnectionError("connection reset")) is None


def test_permanente_nao_dispara_o_circuit_breaker():
    acc = _Acumulador()
    for _ in range(LIMITE_FALHA_TOTAL * 3):
        acc.registra_erro(_ComStatus(400), permanente=True)
    assert acc.erros == acc.permanentes == LIMITE_FALHA_TOTAL * 3


def test_falha_de_servico_ainda_dispara_o_circuit_breaker():
    acc = _Acumulador()
    with pytest.raises(_FalhaTotal):
        for _ in range(LIMITE_FALHA_TOTAL):
            acc.registra_erro(_ComStatus(401))


# ── Download que falha não é documento ilegível ──────────────────────────────────────

def test_sem_arquivo_publicado_devolve_lista_vazia(monkeypatch):
    """O órgão não publicou arquivo do tipo: não há o que ler, e repetir não muda."""
    from pesquisa_precos.core.collection import fetch_files
    from pesquisa_precos.steps import e5_extract

    monkeypatch.setattr(fetch_files, "listar_arquivos", lambda *a, **k: [])
    monkeypatch.setattr(fetch_files, "selecionar_do_tipo", lambda *a, **k: [])
    item0 = {"tipo_doc": "contrato", "orgao_cnpj": "1", "ano": "2026",
             "numero_sequencial": "8", "numero_sequencial_ata": ""}
    assert e5_extract._baixar_documento(item0, ".") == []


def test_arquivo_publicado_que_nao_baixa_vira_erro(monkeypatch):
    """O caso dos 1.274 de 2026-08-30: virava `ilegivel` sem o modelo ter visto nada."""
    from pesquisa_precos.core.collection import fetch_files
    from pesquisa_precos.steps import e5_extract

    monkeypatch.setattr(fetch_files, "listar_arquivos", lambda *a, **k: [{"x": 1}])
    monkeypatch.setattr(fetch_files, "selecionar_do_tipo", lambda *a, **k: [{"x": 1}])
    monkeypatch.setattr(fetch_files, "baixar_arquivos", lambda *a, **k: [])
    item0 = {"tipo_doc": "contrato", "orgao_cnpj": "1", "ano": "2026",
             "numero_sequencial": "8", "numero_sequencial_ata": ""}
    with pytest.raises(e5_extract.DownloadFalhou):
        e5_extract._baixar_documento(item0, ".")


def test_download_falho_vale_retentativa():
    """Ele PODE dar certo amanhã — não pode sair da trilha como o 400 do parser sai."""
    from pesquisa_precos.steps.e5_extract import DownloadFalhou

    assert falha_permanente(DownloadFalhou("nenhum arquivo baixou")) is None
