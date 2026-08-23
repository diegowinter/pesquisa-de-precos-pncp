"""
Guarda das Fases 10 (bloco D) e 11: etapas 5, 6a-6c e 8 no banco, com cômputo externalizável.

O que estes testes protegem:
  1. o XLSX ir para `export.conteudo` e voltar íntegro — é o que substitui o arquivo em disco
     (ADR-018 §2), e um export corrompido só apareceria na mão do usuário final;
  2. a estratégia `visao` receber IMAGENS em vez de uma pasta de PDFs — a mudança que permite
     tirar o PyMuPDF do container sem quebrar a rota de exceção (ADR-019);
  3. as capacidades `pdf`/`pareamento` exigirem um serviço configurado — desde a ADR-021 não
     há caminho em processo, e cair num silenciosamente seria pôr GPU e PyMuPDF no processo
     que só deveria orquestrar.

O motor de pareamento mudou de repositório junto com a implementação: os testes dele agora
vivem em `../pncp-servicos-locais/tests/test_pareamento.py`.

Os testes de banco são PULADOS sem Postgres; os demais rodam sempre.
"""

import pytest

from pesquisa_precos.db import session as db

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)


# ── Estratégia `visao` recebe imagens, não pasta (ADR-019) ───────────────────────────

def test_visao_consome_imagens_prontas():
    from pesquisa_precos.strategies import vision

    class CuradorFake:
        def __init__(self):
            self.chamadas = []

        def extrair_tabela_pdf(self, png):
            self.chamadas.append(png)
            return [{"descricao": f"item de {png.decode()}"}]

    curador = CuradorFake()
    tabela = vision.extrair_tabela(curador, [b"pagina1", b"pagina2"])
    assert len(curador.chamadas) == 2, "uma chamada por página — nunca o documento inteiro"
    assert len(tabela) == 2


def test_visao_respeita_o_teto_de_paginas():
    from pesquisa_precos.strategies import vision

    class CuradorFake:
        def __init__(self):
            self.chamadas = 0

        def extrair_tabela_pdf(self, png):
            self.chamadas += 1
            return []

    curador = CuradorFake()
    vision.extrair_tabela(curador, [b"1", b"2", b"3", b"4"], max_paginas=2)
    assert curador.chamadas == 2


def test_visao_nao_derruba_o_documento_por_uma_pagina_ruim():
    from pesquisa_precos.strategies import vision

    class CuradorFake:
        def extrair_tabela_pdf(self, png):
            if png == b"ruim":
                raise RuntimeError("modelo recusou a imagem")
            return [{"descricao": "ok"}]

    tabela = vision.extrair_tabela(CuradorFake(), [b"boa", b"ruim", b"boa"])
    assert len(tabela) == 2


# ── Capacidades exigem provedor cadastrado (Fase 11 + Fase 14) ───────────────────────
#
# Estes testes protegiam a ADR-021 pelo `.env` (`PDF_BASE_URL` vazio → falha). A ADR-022 tirou
# o `.env` da resolução, então a MESMA invariante é verificada pelo banco: sem provedor
# apontado, ou com provedor sem endereço, a etapa para antes de começar. O que não pode
# acontecer, em nenhum dos dois mundos, é cair num caminho em processo.

@pytest.fixture
def provedor_de_teste():
    """Escreve direto no repo (não pelo service) porque alguns casos precisam de uma linha que
    o service RECUSA gravar — é o cenário "alguém editou a tabela na mão"."""
    from pesquisa_precos.db.repos import execution as repo

    criados: list[str] = []
    # Ver `_snapshot_capacidades` em test_provedores_crud.py: apontar capacidade REAL para um
    # provedor de teste e depois apagar deixaria a instalação sem configuração.
    from sqlalchemy import text as _text
    with db.session() as sessao:
        antes = [dict(r) for r in sessao.execute(_text(
            "SELECT capacidade, provedor, modelo, fallback FROM capacidade_provedor"
        )).mappings()]

    def criar(nome: str, capacidades: list[str], base_url: str):
        with db.session() as sessao:
            repo.upsert_provedor(sessao, nome, capacidades, base_url)
            for c in capacidades:
                repo.apontar_capacidade(sessao, c, nome)
        criados.append(nome)

    yield criar

    from sqlalchemy import text

    with db.session() as sessao:
        for nome in criados:
            sessao.execute(text("DELETE FROM capacidade_provedor WHERE provedor = :n"),
                           {"n": nome})
            sessao.execute(text("DELETE FROM provedor_status WHERE provedor = :n"), {"n": nome})
            sessao.execute(text("DELETE FROM provedor WHERE nome = :n"), {"n": nome})
        for linha in antes:
            repo.apontar_capacidade(sessao, linha["capacidade"], linha["provedor"],
                                    linha["modelo"], linha["fallback"])


@pytestmark_db
@pytest.mark.parametrize("capacidade", ["pdf", "pareamento", "rerank", "embed", "chat"])
def test_capacidade_sem_provedor_falha_em_vez_de_rodar_aqui(capacidade):
    """ADR-021 + ADR-022: não existe adapter em processo NEM fallback para o `.env`. Sem
    provedor apontado, a etapa PARA com uma mensagem que diz o que configurar — em vez de
    carregar torch/PyMuPDF no processo que só deveria baixar e gravar no banco."""
    from sqlalchemy import text

    from pesquisa_precos.providers.resolver import (
        CapacidadeNaoConfigurada,
        Provedores,
        resolver_capacidade,
    )

    with db.session() as sessao:
        # garante a ausência de apontamento, sem tocar no que o operador configurou de verdade
        existente = sessao.execute(
            text("SELECT provedor FROM capacidade_provedor "
                 "WHERE capacidade = CAST(:c AS capacidade)"), {"c": capacidade}).first()
        if existente:
            pytest.skip(f"`{capacidade}` está configurada nesta instalação ({existente[0]})")
        with pytest.raises(CapacidadeNaoConfigurada) as exc:
            resolver_capacidade(capacidade, sessao=sessao)
    assert capacidade in str(exc.value) and "/providers" in str(exc.value)
    with pytest.raises(CapacidadeNaoConfigurada):
        assert getattr(Provedores(), capacidade)


@pytestmark_db
def test_capacidades_viram_remotas_com_provedor_apontado(provedor_de_teste):
    from pesquisa_precos.providers.resolver import Provedores

    provedor_de_teste("teste-d-pdf", ["pdf"], "http://gpu:8200")
    provedor_de_teste("teste-d-par", ["pareamento"], "http://gpu:8300")
    p = Provedores()
    assert type(p.pdf).__name__ == "PdfRemotoAdapter"
    assert type(p.pareamento).__name__ == "PareamentoRemotoAdapter"


@pytestmark_db
def test_provedor_sem_base_url_reprova(provedor_de_teste):
    """Linha gravada na mão, com `base_url` vazia: `_exigir_servico` tem de recusar. Vazio
    NUNCA volta a significar "roda aqui" (ADR-021)."""
    from pesquisa_precos.providers.resolver import CapacidadeNaoConfigurada, Provedores

    provedor_de_teste("teste-d-vazio", ["pdf"], "")
    with pytest.raises(CapacidadeNaoConfigurada, match="base_url"):
        assert Provedores().pdf


@pytestmark_db
def test_health_check_reprova_capacidade_sem_provedor():
    """Sem provedor a etapa não pode começar, e o health check pré-play é onde isso aparece —
    antes de gastar. Tem de virar linha vermelha, não exceção: é o painel de diagnóstico."""
    from sqlalchemy import text

    from pesquisa_precos.providers import health

    with db.session() as sessao:
        if sessao.execute(text("SELECT 1 FROM capacidade_provedor "
                               "WHERE capacidade = 'pdf'")).first():
            pytest.skip("`pdf` está configurada nesta instalação")
    resultado = health.checar_capacidade("pdf")
    assert resultado["saudavel"] is False
    assert resultado["origem"] == "não configurado"


def test_nenhum_adapter_em_processo_sobreviveu():
    """Guarda da ADR-021: um adapter "em processo" reintroduzido traria torch/PyMuPDF de volta
    para o processo que orquestra, e faria isso silenciosamente — só a conta de memória do
    servidor acusaria."""
    import inspect

    from pesquisa_precos.providers import adaptadores

    nomes = [n for n, o in vars(adaptadores).items()
             if inspect.isclass(o) and n.endswith("Adapter")]
    assert not [n for n in nomes if "Processo" in n], nomes


def test_health_check_reprova_capacidade_sem_servico(monkeypatch):
    """Sem `base_url` a etapa não pode começar: o health check pré-play é onde isso aparece,
    antes de gastar. Era o inverso enquanto existia caminho em processo."""
    from pesquisa_precos.providers import health

    monkeypatch.setenv("PDF_BASE_URL", "")
    resultado = health.checar_capacidade("pdf")
    assert resultado["saudavel"] is False


# ── Export no banco (ADR-018 §2) ─────────────────────────────────────────────────────

def test_xlsx_e_gerado_em_memoria():
    """`montar_xlsx` devolve bytes de um XLSX válido — nenhum arquivo em disco."""
    import io
    import zipfile

    from pesquisa_precos.steps.e8_export import COLUNAS_PLASEG, montar_xlsx

    linha = dict.fromkeys(COLUNAS_PLASEG, "x")
    conteudo = montar_xlsx([linha])
    assert conteudo[:2] == b"PK", "XLSX é um zip — o cabeçalho prova que o arquivo é válido"
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        assert "xl/workbook.xml" in z.namelist()


@pytestmark_db
def test_export_vai_e_volta_do_banco():
    from sqlalchemy import text

    from pesquisa_precos.db.repos import execution as repo_exec
    from pesquisa_precos.db.repos import grupo as repo_grupo
    from pesquisa_precos.steps.e8_export import COLUNAS_PLASEG, montar_xlsx

    conteudo = montar_xlsx([dict.fromkeys(COLUNAS_PLASEG, "y")])
    with db.session() as s:
        run_id = repo_exec.run_aberto_ou_criar(s, "teste_bloco_d")
        export_id = repo_grupo.registrar_export(
            s, run_id, "completo", None, 1, 1, "hash-de-teste",
            conteudo=conteudo, nome_arquivo="itens_plaseg.xlsx")
        s.commit()
        try:
            nome, bytes_de_volta = repo_grupo.conteudo_export(s, export_id)
            assert nome == "itens_plaseg.xlsx"
            assert bytes_de_volta == conteudo, "o XLSX tem que voltar byte a byte"
            caminho = s.execute(text("SELECT arquivo FROM export WHERE id = :i"),
                                {"i": export_id}).scalar_one()
            assert caminho is None, "no caminho banco não existe arquivo em disco"
        finally:
            s.execute(text("DELETE FROM export WHERE id = :i"), {"i": export_id})
            s.commit()


# ── `estimar` tem que consultar o banco, não os CSVs locais ─────────────────────────

@pytestmark_db
@pytest.mark.parametrize("key", ["5", "6a", "6b", "6c"])
def test_estimar_usa_o_banco_e_nao_os_csvs(key, monkeypatch):
    """Regressão: `estimar` já leu `data/*.csv` em vez do banco. O sintoma era grosseiro —
    `estimar 6a` reportava 154 MILHÕES de pares num banco vazio, porque somava o produto
    cartesiano do acervo em disco. É o número que o operador usa para decidir se gasta; errar
    aqui é pior que falhar.

    Até a Fase 13 a guarda era o marcador `detalhes["fonte"] == "banco"`, que distinguia os
    dois caminhos. Sem `--fonte`, a guarda passa a ser direta: qualquer leitura de arquivo de
    dados durante `estimar` explode.
    """
    import pandas as pd

    from pesquisa_precos.steps import registry
    from pesquisa_precos.runner.null_context import NullContext

    def proibido(*a, **kw):
        raise AssertionError(f"etapa {key}: `estimar` leu arquivo em vez de consultar o banco")

    monkeypatch.setattr(pd, "read_csv", proibido)
    monkeypatch.setattr(pd, "read_parquet", proibido)

    # `config` real (e não `{}`): a 6c consulta `model_pass1` para montar a estimativa de custo.
    ctx = NullContext(key)
    modulo = registry.obter(key).carregar()
    assert "fonte" not in modulo.Params.model_fields, "o campo `fonte` saiu na Fase 13"
    modulo.estimate(modulo.Params(), ctx)
