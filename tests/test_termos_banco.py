"""
Guarda da Fase 10 bloco B, step 1: geração de termos com `--fonte banco`.

O que estes testes protegem:
  1. `termo_geracao` guardar o termo CRU — se guardasse o expandido, a cascata de categoria
     de `resolver_categorias()` mudaria de resultado em silêncio (é o reason de a tabela
     existir em vez de reconstruir o checkpoint de `termo`/`termo_codigo`);
  2. o resume pular item já gerado — é o que impede repagar a chamada de LLM;
  3. `source='manual'` sobreviver a uma regeração, e termos de LLM saírem de cena
     DESATIVADOS, nunca apagados (apagar levaria junto o watermark da coleta).

Precisa de Postgres com o schema aplicado; PULADO sem ele (padrão de test_config_prompts.py).
"""

import pytest
from sqlalchemy import text

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import termo as repo

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

PDM_TESTE = "999902"
COD_TESTE = "9999031"
TERMO_LLM = "colete balistico teste"
TERMO_MANUAL = "termo manual de teste"


@pytest.fixture
def catalogo_de_teste():
    """Um item de catálogo real (a FK de `termo_geracao` exige) e limpeza dos dois lados."""
    def limpar():
        with db.session() as s:
            s.execute(text("DELETE FROM termo_geracao WHERE codigo = :c"), {"c": COD_TESTE})
            s.execute(text(
                "DELETE FROM termo WHERE termo_norm IN "
                "(SELECT termo_norm FROM termo WHERE termo = ANY(:t))"),
                {"t": [TERMO_LLM, TERMO_MANUAL]})
            s.execute(text("DELETE FROM catalogo_item WHERE codigo = :c"), {"c": COD_TESTE})
            s.execute(text("DELETE FROM catalogo_raw WHERE codigo = :c"), {"c": COD_TESTE})
            s.execute(text("DELETE FROM pdm_permitido WHERE codigo = :c"), {"c": PDM_TESTE})
            s.commit()
    limpar()
    with db.session() as s:
        s.execute(text("""
            INSERT INTO catalogo_item (tipo, codigo, codigo_pdm, nome_pdm, description, active)
            VALUES ('material', :c, :p, 'COLETE', 'COLETE BALISTICO NIVEL III', true)
        """), {"c": COD_TESTE, "p": PDM_TESTE})
        s.commit()
    yield
    limpar()


def _norms_ativos_exceto(sessao, *termos_de_teste: str) -> list[str]:
    """Os `termo_norm` de LLM ativos que NÃO são deste teste.

    `desativar_llm_ausentes` é global por natureza — "o que não veio nesta geração sai de
    cena" — então chamá-la com uma lista curta desativa o acervo inteiro do operador. Em
    2026-08-23 foi exatamente o que aconteceu: um `pytest` contra o banco real derrubou os 87
    termos de uma coleta em andamento, e a etapa 2 passou a morrer com "nenhum termo ativo".
    Passar os termos reais junto mantém o teste testando a MESMA regra, sem vítimas.
    """
    from pesquisa_precos.core.text import normalizar_termo

    reais = sessao.execute(text(
        "SELECT termo_norm FROM termo WHERE active AND coalesce(source, 'llm') <> 'manual'"
    )).scalars().all()
    de_teste = {normalizar_termo(t) for t in termos_de_teste}
    return [n for n in reais if n not in de_teste]



def test_geracao_guarda_o_termo_cru_e_volta_no_formato_do_checkpoint(catalogo_de_teste):
    with db.session() as s:
        repo.gravar_geracao(s, "material", COD_TESTE, ["colete", "colete balistico"],
                            "protecao", model="PASS1", provider="local")
        s.commit()
        checkpoint = repo.geracoes(s)

    assert checkpoint[("material", COD_TESTE)] == {
        "termos": ["colete", "colete balistico"], "categoria": "protecao"}


def test_item_ja_gerado_nao_volta_ao_llm(catalogo_de_teste):
    with db.session() as s:
        assert ("material", COD_TESTE) not in repo.codigos_ja_gerados(s)
        repo.gravar_geracao(s, "material", COD_TESTE, ["colete"], "protecao")
        s.commit()
        assert ("material", COD_TESTE) in repo.codigos_ja_gerados(s)


def test_regravar_o_mesmo_item_substitui_em_vez_de_duplicar(catalogo_de_teste):
    with db.session() as s:
        repo.gravar_geracao(s, "material", COD_TESTE, ["antigo"], "outros")
        repo.gravar_geracao(s, "material", COD_TESTE, ["novo"], "protecao")
        s.commit()
        n = s.execute(text("SELECT count(*) FROM termo_geracao WHERE codigo = :c"),
                      {"c": COD_TESTE}).scalar_one()
        checkpoint = repo.geracoes(s)
    assert n == 1
    assert checkpoint[("material", COD_TESTE)]["termos"] == ["novo"]


def test_categoria_final_vai_para_catalogo_item(catalogo_de_teste):
    with db.session() as s:
        alteradas = repo.gravar_categorias(s, {COD_TESTE: "protecao"})
        s.commit()
        categoria = s.execute(text("SELECT categoria FROM catalogo_item WHERE codigo = :c"),
                              {"c": COD_TESTE}).scalar_one()
    assert alteradas == 1
    assert categoria == "protecao"


def test_gravar_categoria_igual_nao_conta_como_alteracao(catalogo_de_teste):
    """`IS DISTINCT FROM` no UPDATE: rodar a step 1 duas vezes sem mudança não deve
    reportar milhares de códigos 'alterados' nem carimbar `updated_at` à toa."""
    with db.session() as s:
        repo.gravar_categorias(s, {COD_TESTE: "protecao"})
        s.commit()
        segunda = repo.gravar_categorias(s, {COD_TESTE: "protecao"})
        s.commit()
    assert segunda == 0


def test_termo_manual_sobrevive_a_regeracao(catalogo_de_teste):
    with db.session() as s:
        id_manual = repo.upsert(s, TERMO_MANUAL, "protecao", "manual")
        id_llm = repo.upsert(s, TERMO_LLM, "protecao", "llm")
        s.commit()

        # Regeração que NÃO produziu nenhum dos dois termos (mas preserva o acervo real).
        repo.desativar_llm_ausentes(s, _norms_ativos_exceto(s, TERMO_LLM, TERMO_MANUAL))
        s.commit()
        estados = dict(s.execute(text(
            "SELECT id, active FROM termo WHERE id = ANY(:ids)"),
            {"ids": [id_manual, id_llm]}).all())

    assert estados[id_manual] is True, "curadoria humana não pode ser desativada pela step"
    assert estados[id_llm] is False


def test_termo_de_llm_regerado_continua_ativo(catalogo_de_teste):
    from pesquisa_precos.core.text import normalizar_termo

    with db.session() as s:
        id_llm = repo.upsert(s, TERMO_LLM, "protecao", "llm")
        s.commit()
        repo.desativar_llm_ausentes(
            s, [normalizar_termo(TERMO_LLM), *_norms_ativos_exceto(s, TERMO_LLM)])
        s.commit()
        active = s.execute(text("SELECT active FROM termo WHERE id = :i"),
                          {"i": id_llm}).scalar_one()
    assert active is True


def test_termo_desativado_nao_e_apagado(catalogo_de_teste):
    """Apagar o termo levaria junto `collection_watermark` (ON DELETE CASCADE) — e perder o
    watermark significa re-varrer o PNCP inteiro na próxima atualização."""
    with db.session() as s:
        id_llm = repo.upsert(s, TERMO_LLM, "protecao", "llm")
        s.commit()
        repo.desativar_llm_ausentes(s, _norms_ativos_exceto(s, TERMO_LLM))
        s.commit()
        existe = s.execute(text("SELECT count(*) FROM termo WHERE id = :i"),
                           {"i": id_llm}).scalar_one()
    assert existe == 1
