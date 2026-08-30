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


# ── O lock e o worker pendurado ──────────────────────────────────────────────────────

def test_lock_registra_o_pid_real_do_worker():
    """O lock nasce com `pid=0` (é tomado antes de existir processo) e alguém precisa voltar
    para corrigir. Sem isso o `run_lock` — primeiro lugar onde se olha quando a etapa trava —
    mente `pid = 0`, e achar o worker exige listar processos do sistema operacional."""
    import inspect

    from pesquisa_precos.runner import launcher, lock

    assert hasattr(lock, "registrar_pid")
    fonte = inspect.getsource(launcher.iniciar_subprocesso)
    assert "registrar_pid(sessao, run_etapa_id, processo.pid)" in fonte


# ── SQL que só quebra em produção ────────────────────────────────────────────────────

@pytest.mark.skipif(not __import__("pesquisa_precos.db.session", fromlist=["is_available"])
                    .is_available()[0], reason="sem banco")
def test_gravar_veredito_executa_no_postgres():
    """`gravar_veredito` só roda na etapa 6c, que é PAGA — um erro de SQL aqui aparece depois
    de o LLM ter sido cobrado. Foi o que houve em 2026-08-30: o alias declarava `model` e o
    SET lia `v.modelo` (resquício da renomeação para inglês), e 520 vereditos pagos se
    perderam na gravação.

    Executa contra uma chave inexistente e desfaz: valida o SQL sem tocar em dado.
    """
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import par as repo

    with db.session() as sessao:
        assert repo.gravar_veredito(
            sessao, [("__par_que_nao_existe__", "sim", "j", "m")]) == 0
        sessao.rollback()


def test_veredito_da_6c_le_a_string_e_nao_a_verdade_booleana():
    """`comparar_par` devolve "sim"/"nao"/"erro". Testar `if res["mesmo_item"]` faz "nao" —
    string não vazia — valer VERDADEIRO, e todo par vira `sim`. Aconteceu em 2026-08-30: os
    893 ambíguos saíram `sim`, com justificativas dizendo "naturezas distintas"."""
    import pathlib
    import re

    fonte = pathlib.Path("pesquisa_precos/steps/e6c_validate.py").read_text(encoding="utf-8")
    corpo = "\n".join(ln for ln in fonte.splitlines() if not re.match(r"\s*#", ln))
    assert 'if res.get("mesmo_item") else' not in corpo
    assert 'mesmo not in ("sim", "nao")' in corpo


def test_toda_etapa_devolve_step_result():
    """`run()` sem `return` devolve `None`, e o worker quebra em `resultado.metrics` DEPOIS de
    a etapa ter feito e gravado todo o trabalho. A etapa 7 caía assim (2026-08-30): gravou
    1.065 linhas em `grupo_item` e a tela marcou "falhou".

    A checagem é sintática: o último comando de `run()` tem de ser `return` (ou `raise`), o
    que impede a função de "cair pelo fim" devolvendo `None`.
    """
    import ast
    import inspect
    import textwrap

    from pesquisa_precos.steps import registry

    faltando = []
    for definicao in registry.ETAPAS:
        modulo = definicao.carregar()
        arvore = ast.parse(textwrap.dedent(inspect.getsource(modulo.run)))
        ultimo = arvore.body[0].body[-1]
        if not isinstance(ultimo, (ast.Return, ast.Raise)):
            faltando.append(f"{definicao.key} ({modulo.__name__}): termina em "
                            f"{type(ultimo).__name__}")
    assert not faltando, "run() pode cair pelo fim devolvendo None: " + "; ".join(faltando)


def test_xlsx_aceita_caractere_de_controle_do_pdf():
    """O XLSX proíbe caracteres de controle; o Postgres só proíbe o NUL. Um `\x13` no lugar
    de um travessão atravessou coleta, extração, pareamento e agrupamento, e derrubou a
    etapa 8 — a ÚLTIMA — por UMA linha em 34.256 (2026-08-30).
    """
    from openpyxl import Workbook

    from pesquisa_precos.steps.e8_export import _para_celula

    sujo = "COLETE NIVEL IIIA " + chr(0x13) + " NIJ STD 0101.06"
    limpo = _para_celula(sujo)
    assert chr(0x13) not in limpo
    assert "COLETE NIVEL IIIA" in limpo and "NIJ STD" in limpo
    # o que importa é o openpyxl aceitar, não a nossa regex concordar consigo mesma
    Workbook().active.append([limpo])
    assert _para_celula(3.5) == 3.5 and _para_celula(None) is None
