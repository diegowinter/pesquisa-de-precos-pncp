"""
Guarda da Fase 10 bloco C: etapas 2, 3 e 4 com `--fonte banco`.

O que estes testes protegem:
  1. o documento levar `numero_sequencial`/`numero_sequencial_ata` (ADR-012) — sem eles a
     etapa 5 não refaz `listar_arquivos()`, e o sintoma só apareceria dois blocos adiante;
  2. `data_atualizacao_pncp` chegar ao banco — é o watermark; perdê-lo custa uma varredura
     completa do PNCP na atualização seguinte;
  3. o progresso da coleta NÃO ser derivado do resultado (busca sem documento é busca feita);
  4. o dedup por texto da etapa 3 ser permanente entre runs (ADR-007);
  5. a etapa 4 desmarcar quem perdeu categoria, não só marcar.

Precisa de Postgres com o schema aplicado; PULADO sem ele.
"""

import pytest
from sqlalchemy import text

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import classification as repo_cls
from pesquisa_precos.db.repos import documento as repo
from pesquisa_precos.steps.e2_collect import _linha_documento, _linha_item

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

NC = "99999999000191-1-000001/2099"      # fora de qualquer faixa real
ITEM_KEY = f"{NC}::1"
TERMO_TESTE = "termo de teste bloco c"

LINHA_ITEM = {
    "item_key": ITEM_KEY, "tipo_doc": "contrato", "numeroControlePNCP": NC,
    "numeroItem": "1", "descricao_api": "COLETE BALISTICO NIVEL III-A",
    "unidade": "UNIDADE", "quantidade": "10", "preco_unitario": "1234.56",
    "preco_estimado": "1300.00", "fornecedor": "FORNECEDOR TESTE",
    "data_resultado": "2026-01-15", "orgao": "ORGAO TESTE", "orgao_cnpj": "99999999000191",
    "uf": "PR", "data": "2026-01-10", "ano": "2026",
    "data_assinatura": "2026-01-12", "data_fim_vigencia": "2027-01-12",
    "numero_sequencial": "42", "numero_sequencial_ata": None,
    "url_pncp": "https://pncp.gov.br/app/contratos/teste",
}


# ── Mapeamento (puro) ────────────────────────────────────────────────────────────────

def test_documento_leva_os_identificadores_que_a_etapa_5_precisa():
    """ADR-012: sem `numero_sequencial` a etapa 5 só consegue rebaixar pela url pública."""
    linha = _linha_documento(LINHA_ITEM, "2026-02-01T10:00:00", 3)
    assert linha[11] == "42"                     # numero_sequencial
    assert linha[10] == LINHA_ITEM["url_pncp"]   # url_pncp
    assert linha[13] == 3                        # n_itens


def test_documento_leva_a_data_de_atualizacao_que_vira_watermark():
    linha = _linha_documento(LINHA_ITEM, "2026-02-01T10:00:00", 1)
    assert linha[9] == "2026-02-01T10:00:00"


def test_data_invalida_vira_null_em_vez_de_derrubar_o_lote():
    """Data ruim num documento não pode custar a coleta de outros mil."""
    linha = _linha_documento({**LINHA_ITEM, "data": "31/02/2026"}, None, 1)
    assert linha[6] is None


def test_item_calcula_texto_hash_na_ingestao():
    """O hash vem da ingestão, nunca da hora de classificar — é ele que faz o dedup da
    etapa 3 ser uma consulta em vez de um agrupamento de 1,6 milhão de linhas."""
    from pesquisa_precos.core.text import texto_hash

    linha = _linha_item(LINHA_ITEM)
    assert linha[10] == texto_hash(LINHA_ITEM["descricao_api"], LINHA_ITEM["unidade"])
    assert linha[0] == ITEM_KEY


def test_numero_e_preco_viram_tipos_do_banco():
    from decimal import Decimal

    linha = _linha_item(LINHA_ITEM)
    assert linha[2] == 1                          # numero_item int
    assert linha[6] == Decimal("1234.56")         # preco_unitario Decimal, nunca float


# ── Banco ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def coleta_limpa():
    def limpar():
        with db.session() as s:
            s.execute(text("DELETE FROM item WHERE item_key LIKE :p"), {"p": f"{NC}%"})
            s.execute(text("DELETE FROM coleta_pendente WHERE numero_controle_pncp = :n"),
                      {"n": NC})
            s.execute(text("DELETE FROM documento WHERE numero_controle_pncp = :n"), {"n": NC})
            s.execute(text("DELETE FROM termo WHERE termo = :t"), {"t": TERMO_TESTE})
            s.commit()
    limpar()
    yield
    limpar()


def _semear_documento(sessao, n_itens=1):
    from pesquisa_precos.db.repos import documento as r

    with db.raw_connection() as conn:
        r.gravar_documentos(conn, [_linha_documento(LINHA_ITEM, "2026-02-01T10:00:00", n_itens)])
        r.gravar_itens(conn, [_linha_item(LINHA_ITEM)])
        conn.commit()


def test_documento_e_item_gravados_sobrevivem_a_releitura(coleta_limpa):
    with db.session() as s:
        _semear_documento(s)
        seq, atu = s.execute(text(
            "SELECT numero_sequencial, data_atualizacao_pncp FROM documento "
            " WHERE numero_controle_pncp = :n"), {"n": NC}).one()
        assert seq == "42"
        assert atu is not None
        assert NC in repo.controles_conhecidos(s)


def test_progresso_da_busca_nao_e_derivado_do_resultado(coleta_limpa):
    """Uma busca legítima pode não trazer documento nenhum. Se o progresso fosse derivado de
    `documento`, esses termos seriam revarridos para sempre."""
    from pesquisa_precos.db.repos import termo as repo_termo

    with db.session() as s:
        termo_id = repo_termo.upsert(s, TERMO_TESTE, "protecao", "llm")
        s.commit()

        assert (termo_id, "contrato") not in repo.buscas_concluidas(s)
        repo.marcar_busca(s, termo_id, "contrato", n_documentos=0, n_itens=0)
        s.commit()
        assert (termo_id, "contrato") in repo.buscas_concluidas(s)


def test_marcar_busca_acumula_em_vez_de_zerar(coleta_limpa):
    from pesquisa_precos.db.repos import termo as repo_termo

    with db.session() as s:
        termo_id = repo_termo.upsert(s, TERMO_TESTE, None, "llm")
        repo.marcar_busca(s, termo_id, "contrato", 2, 10)
        repo.marcar_busca(s, termo_id, "contrato", 3, 5)
        s.commit()
        docs, itens = s.execute(text(
            "SELECT n_documentos, n_itens FROM coleta_progresso WHERE termo_id = :i"),
            {"i": termo_id}).one()
    assert (docs, itens) == (5, 15)


def test_pendente_entra_e_sai_da_fila_de_revisita(coleta_limpa):
    with db.session() as s:
        repo.gravar_pendente(s, NC, "contrato", {"numeroControlePNCP": NC, "ano": 2026})
        s.commit()
        pend = repo.pendentes(s)
        assert NC in pend
        assert pend[NC]["base"]["ano"] == 2026, "o dict da busca volta inteiro para a revisita"

        repo.remover_pendente(s, NC)
        s.commit()
        assert NC not in repo.pendentes(s)


# ── Etapa 3: dedup permanente ────────────────────────────────────────────────────────

def test_texto_ja_classificado_nao_volta_como_pendente(coleta_limpa):
    """ADR-007: o dedup deixa de ser intra-execução e vira permanente entre runs."""
    from pesquisa_precos.core.text import texto_hash

    h = texto_hash(LINHA_ITEM["descricao_api"], LINHA_ITEM["unidade"])
    with db.session() as s:
        _semear_documento(s)
        pendentes_antes = {t["texto_hash"] for t in repo_cls.textos_pendentes(s)}
        assert h in pendentes_antes

    with db.raw_connection() as conn:
        repo_cls.gravar(conn, [(h, LINHA_ITEM["descricao_api"], LINHA_ITEM["unidade"],
                                ["protecao"], repo_cls.confianca_para_real("alta"),
                                None, "PASS1", "local", None)])
        conn.commit()
    try:
        with db.session() as s:
            assert h not in {t["texto_hash"] for t in repo_cls.textos_pendentes(s)}
    finally:
        with db.session() as s:
            s.execute(text("DELETE FROM texto_classificacao WHERE texto_hash = :h"), {"h": h})
            s.commit()


# ── Etapa 4: sobrevivente é atributo, nos dois sentidos ──────────────────────────────

def test_etapa_4_marca_e_desmarca(coleta_limpa):
    """Marcar sem desmarcar deixaria item reprovado numa reclassificação marcado para sempre."""
    with db.session() as s:
        _semear_documento(s)
        s.execute(text("INSERT INTO item_categoria (item_key, categoria) VALUES (:k, 'protecao')"
                       " ON CONFLICT DO NOTHING"), {"k": ITEM_KEY})
        s.commit()
        repo.marcar_sobreviventes_por_categoria(s)
        s.commit()
        assert s.execute(text("SELECT sobrevivente FROM item WHERE item_key = :k"),
                         {"k": ITEM_KEY}).scalar_one() is True

        s.execute(text("DELETE FROM item_categoria WHERE item_key = :k"), {"k": ITEM_KEY})
        s.commit()
        resultado = repo.marcar_sobreviventes_por_categoria(s)
        s.commit()
        assert resultado["desmarcados"] >= 1
        assert s.execute(text("SELECT sobrevivente FROM item WHERE item_key = :k"),
                         {"k": ITEM_KEY}).scalar_one() is False


# ── Escala de confiança (o bug que a coluna `real` denunciou) ────────────────────────

def test_confianca_palavra_vira_numero():
    """O LLM devolve 'alta'/'media'/'baixa'; a coluna é `real`. Sem a conversão, a etapa 3
    quebra no PRIMEIRO lote — foi assim que este teste nasceu."""
    assert repo_cls.confianca_para_real("alta") == 1.0
    assert repo_cls.confianca_para_real("MÉDIA") == 0.6
    assert repo_cls.confianca_para_real("baixa") == 0.3


def test_confianca_erro_e_desconhecida_viram_null():
    """'erro' não é uma classificação de baixa confiança: é a marca de uma chamada que falhou."""
    assert repo_cls.confianca_para_real("erro") is None
    assert repo_cls.confianca_para_real("qualquer coisa") is None
    assert repo_cls.confianca_para_real("") is None


def test_escala_da_migracao_e_a_mesma_da_etapa():
    """Duas tabelas de conversão divergindo fariam o mesmo texto ter confiança diferente
    conforme tivesse vindo do CSV migrado ou da etapa."""
    from migracao.m08_classificacao import CONFIANCA

    assert CONFIANCA is repo_cls.CONFIANCA_ORDINAL
