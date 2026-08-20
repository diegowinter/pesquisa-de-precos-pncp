"""
Guarda das Fases 10 (bloco D) e 11: etapas 5, 6a-6c e 8 no banco, com cômputo externalizável.

O que estes testes protegem:
  1. o XLSX ir para `export.conteudo` e voltar íntegro — é o que substitui o arquivo em disco
     (ADR-018 §2), e um export corrompido só apareceria na mão do usuário final;
  2. a estratégia `visao` receber IMAGENS em vez de uma pasta de PDFs — a mudança que permite
     tirar o PyMuPDF do container sem quebrar a rota de exceção (ADR-019);
  3. as capacidades `pdf`/`pareamento` alternarem entre em-processo e remoto pela config, sem
     a etapa saber qual está em uso;
  4. o motor de pareamento preservar o corte em streaming e o desempate estável — a regra que
     não pode se perder ao mudar de lado.

Os testes de banco são PULADOS sem Postgres; os demais rodam sempre.
"""

import pytest

from pesquisa_precos.core.pareamento import motor
from pesquisa_precos.db import sessao as db

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.url_banco()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.esta_disponivel()[0], reason=_MOTIVO_SEM_BANCO)


# ── Motor de pareamento (puro) ───────────────────────────────────────────────────────

CATALOGO = [{"codigo": "C1", "texto": "colete balistico", "categoria": "protecao"}]
ITENS = [
    {"item_key": "i0", "descricao_final": "colete balistico nivel III-A", "categoria": "protecao"},
    {"item_key": "i1", "descricao_final": "colete a prova de balas nivel 3", "categoria": "protecao"},
    {"item_key": "i2", "descricao_final": "capacete balistico", "categoria": "protecao"},
    {"item_key": "i3", "descricao_final": "cadeira de escritorio giratoria", "categoria": "protecao"},
    {"item_key": "i4", "descricao_final": "papel sulfite A4", "categoria": "protecao"},
    {"item_key": "i5", "descricao_final": "caneta esferografica azul", "categoria": "protecao"},
]


def test_pareamento_corta_pelo_piso():
    pares = motor.parear(CATALOGO, ITENS, piso=0.3)
    sobreviventes = {p["item_key"] for p in pares}
    assert "i0" in sobreviventes, "o item mais parecido tem que sobreviver"
    assert "i4" not in sobreviventes and "i5" not in sobreviventes


def test_pareamento_respeita_top_k():
    assert len(motor.parear(CATALOGO, ITENS, piso=0.0, top_k=2)) == 2


def test_pareamento_e_deterministico():
    """`argsort(kind='stable')` reproduz o desempate do `rank(method='first')` original. Sem
    ele, dois pares de score idêntico trocariam de posição entre execuções e o top-K
    devolveria conjuntos diferentes para a MESMA entrada."""
    a = [p["par_key"] for p in motor.parear(CATALOGO, ITENS, piso=0.0, top_k=3)]
    b = [p["par_key"] for p in motor.parear(CATALOGO, ITENS, piso=0.0, top_k=3)]
    assert a == b


def test_pareamento_nao_cruza_categorias():
    """O produto é RESTRITO à mesma categoria — regra de negócio da 6a."""
    itens = [{"item_key": "x", "descricao_final": "colete balistico", "categoria": "armamento"}]
    assert motor.parear(CATALOGO, itens, piso=0.0) == []


def test_pareamento_sem_embedding_usa_so_bm25():
    """Equivalente ao `--sem-embedding`: sem GPU, o cosseno é zero e o BM25 decide sozinho."""
    pares = motor.parear(CATALOGO, ITENS, piso=0.0, top_k=1, embed=None)
    assert pares and pares[0]["score_cosseno"] == 0.0


# ── Estratégia `visao` recebe imagens, não pasta (ADR-019) ───────────────────────────

def test_visao_consome_imagens_prontas():
    from pesquisa_precos.estrategias import visao

    class CuradorFake:
        def __init__(self):
            self.chamadas = []

        def extrair_tabela_pdf(self, png):
            self.chamadas.append(png)
            return [{"descricao": f"item de {png.decode()}"}]

    curador = CuradorFake()
    tabela = visao.extrair_tabela(curador, [b"pagina1", b"pagina2"])
    assert len(curador.chamadas) == 2, "uma chamada por página — nunca o documento inteiro"
    assert len(tabela) == 2


def test_visao_respeita_o_teto_de_paginas():
    from pesquisa_precos.estrategias import visao

    class CuradorFake:
        def __init__(self):
            self.chamadas = 0

        def extrair_tabela_pdf(self, png):
            self.chamadas += 1
            return []

    curador = CuradorFake()
    visao.extrair_tabela(curador, [b"1", b"2", b"3", b"4"], max_paginas=2)
    assert curador.chamadas == 2


def test_visao_nao_derruba_o_documento_por_uma_pagina_ruim():
    from pesquisa_precos.estrategias import visao

    class CuradorFake:
        def extrair_tabela_pdf(self, png):
            if png == b"ruim":
                raise RuntimeError("modelo recusou a imagem")
            return [{"descricao": "ok"}]

    tabela = visao.extrair_tabela(CuradorFake(), [b"boa", b"ruim", b"boa"])
    assert len(tabela) == 2


# ── Capacidades novas alternam por configuração (Fase 11) ────────────────────────────

def test_capacidades_caem_em_processo_sem_servico(monkeypatch):
    from pesquisa_precos.config.settings import carregar_config
    from pesquisa_precos.providers.resolver import Provedores

    monkeypatch.setenv("PDF_BASE_URL", "")
    monkeypatch.setenv("PAREAMENTO_BASE_URL", "")
    p = Provedores(carregar_config())
    assert type(p.pdf).__name__ == "PdfEmProcessoAdapter"
    assert type(p.pareamento).__name__ == "PareamentoEmProcessoAdapter"


def test_capacidades_viram_remotas_com_base_url(monkeypatch):
    from pesquisa_precos.config.settings import carregar_config
    from pesquisa_precos.providers.resolver import Provedores

    monkeypatch.setenv("PDF_BASE_URL", "http://gpu:8200")
    monkeypatch.setenv("PAREAMENTO_BASE_URL", "http://gpu:8300")
    p = Provedores(carregar_config())
    assert type(p.pdf).__name__ == "PdfRemotoAdapter"
    assert type(p.pareamento).__name__ == "PareamentoRemotoAdapter"


def test_health_check_nao_reprova_capacidade_em_processo(monkeypatch):
    """Regressão: ao entrar no registry da etapa 5, `pdf` sem serviço configurado passou a ser
    sondado por rede e reprovava a etapa antes de ela começar — na máquina do usuário, que é
    o modo de sempre."""
    from pesquisa_precos.config.settings import carregar_config
    from pesquisa_precos.providers import saude

    monkeypatch.setenv("PDF_BASE_URL", "")
    resultado = saude.checar_capacidade("pdf", carregar_config())
    assert resultado["saudavel"] is True


# ── Export no banco (ADR-018 §2) ─────────────────────────────────────────────────────

def test_xlsx_e_gerado_em_memoria():
    """`montar_xlsx` devolve bytes de um XLSX válido — nenhum arquivo em disco."""
    import io
    import zipfile

    from pesquisa_precos.etapas.e8_exportar import COLUNAS_PLASEG, montar_xlsx

    linha = dict.fromkeys(COLUNAS_PLASEG, "x")
    conteudo = montar_xlsx([linha])
    assert conteudo[:2] == b"PK", "XLSX é um zip — o cabeçalho prova que o arquivo é válido"
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        assert "xl/workbook.xml" in z.namelist()


@pytestmark_db
def test_export_vai_e_volta_do_banco():
    from sqlalchemy import text

    from pesquisa_precos.db.repos import execucao as repo_exec
    from pesquisa_precos.db.repos import grupo as repo_grupo
    from pesquisa_precos.etapas.e8_exportar import COLUNAS_PLASEG, montar_xlsx

    conteudo = montar_xlsx([dict.fromkeys(COLUNAS_PLASEG, "y")])
    with db.sessao() as s:
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
@pytest.mark.parametrize("chave", ["5", "6a", "6b", "6c"])
def test_estimar_usa_o_banco_e_nao_os_csvs(chave):
    """Regressão: com `--fonte banco` (o default), `estimar` continuava lendo `data/*.csv`.
    O sintoma era grosseiro — `estimar 6a` reportava 154 MILHÕES de pares num banco vazio,
    porque somava o produto cartesiano do acervo em disco. É o número que o operador usa para
    decidir se gasta; errar aqui é pior que falhar.

    A asserção é sobre a ORIGEM, não sobre o número: `detalhes["fonte"] == "banco"` prova que
    o ramo certo rodou, e continua válida quando o banco tiver dados.
    """
    from pesquisa_precos.config.settings import carregar_config
    from pesquisa_precos.etapas import registry
    from pesquisa_precos.runner.contexto_console import ContextoConsole

    # `config` real (e não `{}`): a 6c consulta `model_pass1` para montar a estimativa de custo.
    ctx = ContextoConsole(chave, config=carregar_config(), mostrar_barra=False)
    modulo = registry.obter(chave).carregar()
    params = modulo.Params()
    assert params.fonte == "banco", "o default da Fase 10 é banco"
    estimativa = modulo.estimar(params, ctx)
    assert estimativa.detalhes.get("fonte") == "banco", \
        f"etapa {chave}: `estimar` não passou pelo ramo do banco"
