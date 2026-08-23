"""
Guarda da Fase 10 bloco A/B: curadoria de catálogo no banco (ADR-017) e o checkpoint de
página que substitui a pasta de parquet-partes (ADR-018).

O mapeamento `registro da API → linha de catalogo_raw` é puro e roda sempre; o resto precisa
de Postgres com o schema aplicado e é PULADO sem ele (mesmo padrão de test_config_prompts.py).

O que estes testes protegem, em ordem de importância:
  1. a derivação `catalogo_raw ∩ pdm_permitido` casar material por `codigo_pdm` e serviço por
     `codigo` — a assimetria é herdada da API e é o erro mais fácil de introduzir;
  2. rederivar NÃO apagar `catalogo_item.categoria`, que custa LLM na step 1;
  3. revogar um PDM desativar o item em vez de apagá-lo;
  4. o delta tratar a primeira execução como baseline (delta zero), nunca como "tudo novo".
"""

import pytest
from sqlalchemy import text

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import curation as repo
from pesquisa_precos.steps.e0a_catalogo import _linha_raw

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

# Códigos de teste, fora de qualquer faixa real do CATMAT/CATSER.
PDM_TESTE = "999901"
COD_MATERIAL = "9999011"
COD_SERVICO = "9999021"
COD_INEXISTENTE = "000000inexistente"   # PDM curado que não casa item nenhum


# ── Mapeamento da API (puro) ─────────────────────────────────────────────────────────

def test_linha_raw_material_usa_descricao_item_e_pdm():
    linha = _linha_raw("material", {
        "codigoItem": 123, "codigoPdm": 456, "nomePdm": "ARMA",
        "descricaoItem": "PISTOLA 9MM", "codigoGrupo": 10,
        "nomeGrupo": "ARMAMENTO", "nomeClasse": "PISTOLAS"})
    assert linha == ("material", "123", "456", "ARMA", "PISTOLA 9MM", "10",
                     "ARMAMENTO", "PISTOLAS")


def test_linha_raw_service_nao_tem_pdm_e_usa_nome_servico():
    linha = _linha_raw("servico", {
        "codigoServico": 789, "nomeServico": "MANUTENÇÃO DE VIATURA",
        "codigoGrupo": 841, "nomeGrupo": "MANUTENÇÃO", "nomeClasse": "VEÍCULOS"})
    assert linha == ("servico", "789", None, None, "MANUTENÇÃO DE VIATURA", "841",
                     "MANUTENÇÃO", "VEÍCULOS")


def test_linha_raw_campo_vazio_vira_none_e_nao_string_vazia():
    """`''` e NULL casariam diferente no join da derivação — um PDM vazio não pode virar ''."""
    linha = _linha_raw("material", {"codigoItem": 1, "codigoPdm": "  ", "descricaoItem": "X"})
    assert linha[2] is None


def test_linha_raw_sem_codigo_e_descartada():
    assert _linha_raw("material", {"descricaoItem": "sem código"}) is None
    assert _linha_raw("servico", {"nomeServico": "sem código"}) is None


# ── Curadoria e derivação (banco) ────────────────────────────────────────────────────

@pytest.fixture
def banco_limpo():
    """Remove os códigos de teste antes e depois. Não toca em nada com código real."""
    def limpar():
        with db.session() as s:
            for tabela, coluna, valores in (
                ("catalogo_item", "codigo", [COD_MATERIAL, COD_SERVICO]),
                ("catalogo_raw", "codigo", [COD_MATERIAL, COD_SERVICO]),
                ("pdm_permitido", "codigo", [PDM_TESTE, COD_SERVICO, COD_INEXISTENTE]),
                ("catalogo_download", "prefixo", ["teste_fase10"]),
            ):
                s.execute(text(f"DELETE FROM {tabela} WHERE {coluna} = ANY(:v)"),
                          {"v": valores})
            s.commit()
    limpar()
    yield
    limpar()


def _semear_raw(sessao):
    sessao.execute(text("""
        INSERT INTO catalogo_raw (tipo, codigo, codigo_pdm, nome_pdm, description)
        VALUES ('material', :cm, :pdm, 'PDM DE TESTE', 'ITEM DE TESTE'),
               ('servico', :cs, NULL, NULL, 'SERVICO DE TESTE')
        ON CONFLICT (tipo, codigo) DO NOTHING
    """), {"cm": COD_MATERIAL, "cs": COD_SERVICO, "pdm": PDM_TESTE})


@pytestmark_db
def test_derivacao_casa_material_por_pdm_e_service_por_codigo(banco_limpo):
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE, created_by="teste")
        repo.permitir(s, "servico", COD_SERVICO, created_by="teste")
        repo.derivar_catalogo_item(s)
        s.commit()
        derivados = set(s.scalars(text(
            "SELECT codigo FROM catalogo_item WHERE codigo = ANY(:v) AND active"),
            {"v": [COD_MATERIAL, COD_SERVICO]}).all())
    assert derivados == {COD_MATERIAL, COD_SERVICO}


@pytestmark_db
def test_material_fora_da_allow_list_nao_e_derivado(banco_limpo):
    with db.session() as s:
        _semear_raw(s)
        repo.derivar_catalogo_item(s)   # nenhum permitido cadastrado
        s.commit()
        n = s.execute(text("SELECT count(*) FROM catalogo_item WHERE codigo = :c"),
                      {"c": COD_MATERIAL}).scalar_one()
    assert n == 0


@pytestmark_db
def test_rederivar_preserva_categoria_da_etapa_1(banco_limpo):
    """A categoria custa LLM e não é derivável do catálogo. Um `DO UPDATE` genérico a
    apagaria a cada rederivação — este teste é o que impede essa regressão."""
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.derivar_catalogo_item(s)
        s.execute(text("UPDATE catalogo_item SET categoria = 'armamento' WHERE codigo = :c"),
                  {"c": COD_MATERIAL})
        s.commit()

        repo.derivar_catalogo_item(s)   # segunda passada, como numa recuradoria
        s.commit()
        categoria = s.execute(text("SELECT categoria FROM catalogo_item WHERE codigo = :c"),
                              {"c": COD_MATERIAL}).scalar_one()
    assert categoria == "armamento"


@pytestmark_db
def test_revogar_desativa_o_item_em_vez_de_apagar(banco_limpo):
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.derivar_catalogo_item(s)
        s.commit()

        repo.revogar(s, "material", PDM_TESTE, reason="teste")
        repo.derivar_catalogo_item(s)
        s.commit()
        linha = s.execute(text("SELECT active FROM catalogo_item WHERE codigo = :c"),
                          {"c": COD_MATERIAL}).all()
    assert linha == [(False,)], "o item deve continuar existindo, apenas inativo"


@pytestmark_db
def test_listar_permitidos_sem_filtro_de_tipo(banco_limpo):
    """Regressão: `tipo=None` (listar os dois) fazia o Postgres levantar `AmbiguousParameter`
    por não conseguir inferir o tipo do parâmetro NULL. É o caminho que `estimar 0a` e a tela
    de curadoria usam — ou seja, o caso mais comum, não o exótico."""
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.permitir(s, "servico", COD_SERVICO)
        s.commit()

        todos = repo.listar_permitidos(s)
        so_material = repo.listar_permitidos(s, tipo="material")

    codigos = {p["codigo"] for p in todos}
    assert {PDM_TESTE, COD_SERVICO} <= codigos
    assert all(p["tipo"] == "material" for p in so_material)
    assert {p["codigo"] for p in so_material} >= {PDM_TESTE}


@pytestmark_db
def test_listar_permitidos_conta_itens_do_catalogo(banco_limpo):
    """A contagem por código é o que mostra curadoria morta (PDM que casa 0 itens)."""
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.permitir(s, "material", COD_INEXISTENTE)
        s.commit()
        por_codigo = {p["codigo"]: p["n_itens"] for p in repo.listar_permitidos(s, "material")}

    assert por_codigo[PDM_TESTE] == 1
    assert por_codigo[COD_INEXISTENTE] == 0


@pytestmark_db
def test_permitir_e_idempotente_e_reativa(banco_limpo):
    with db.session() as s:
        repo.permitir(s, "material", PDM_TESTE, observacao="motivo original")
        repo.revogar(s, "material", PDM_TESTE)
        repo.permitir(s, "material", PDM_TESTE)   # de volta ao escopo
        s.commit()
        active, obs = s.execute(text(
            "SELECT active, observacao FROM pdm_permitido WHERE codigo = :c"),
            {"c": PDM_TESTE}).one()
    assert active is True
    assert obs == "motivo original", "reativar não pode apagar o reason registrado"


# ── Checkpoint de página ─────────────────────────────────────────────────────────────

@pytestmark_db
def test_checkpoint_de_pagina_e_o_que_permite_retomar(banco_limpo):
    with db.session() as s:
        assert repo.paginas_baixadas(s, "material", "teste_fase10") == set()
        repo.marcar_pagina(s, "material", "teste_fase10", 1, 500)
        repo.marcar_pagina(s, "material", "teste_fase10", 2, 500)
        s.commit()
        assert repo.paginas_baixadas(s, "material", "teste_fase10") == {1, 2}

        repo.marcar_pagina(s, "material", "teste_fase10", 2, 480)  # idempotente
        s.commit()
        assert repo.paginas_baixadas(s, "material", "teste_fase10") == {1, 2}


@pytestmark_db
def test_prefixo_separa_paginas_de_grupos_diferentes(banco_limpo):
    """`--so-grupos-seguranca` pagina cada grupo do zero: sem o prefixo na PK, a página 1 do
    grupo 10 marcaria a página 1 do grupo 12 como já baixada."""
    with db.session() as s:
        repo.marcar_pagina(s, "material", "teste_fase10", 1, 10)
        s.commit()
        assert repo.paginas_baixadas(s, "material", "teste_fase10_outro") == set()


# ── Grupos de segurança (recorte do download) ────────────────────────────────────────

GRUPO_TESTE = "99991"


@pytest.fixture
def grupos_limpos():
    def limpar():
        with db.session() as s:
            s.execute(text("DELETE FROM grupo_permitido WHERE codigo = :c"),
                      {"c": GRUPO_TESTE})
            s.commit()
    limpar()
    yield
    limpar()


@pytestmark_db
def test_seed_reproduz_os_grupos_que_estavam_no_codigo():
    """A migration 0006 tem que trazer exatamente as constantes de `core/catalogo/local.py`.
    Divergir aqui significa a 0a passar a baixar um recorte diferente do que sempre baixou."""
    from pesquisa_precos.core.catalogo.local import GRUPOS_MATERIAIS, GRUPOS_SERVICOS

    with db.session() as s:
        materiais = set(repo.grupos_ativos(s, "material"))
        servicos = set(repo.grupos_ativos(s, "servico"))

    assert materiais >= {str(g) for g in GRUPOS_MATERIAIS}
    assert servicos >= {str(g) for g in GRUPOS_SERVICOS}


@pytestmark_db
def test_revogar_grupo_tira_do_download_sem_apagar(grupos_limpos):
    with db.session() as s:
        repo.permitir_grupo(s, "material", GRUPO_TESTE, created_by="teste")
        s.commit()
        assert GRUPO_TESTE in repo.grupos_ativos(s, "material")

        repo.revogar_grupo(s, "material", GRUPO_TESTE, reason="teste")
        s.commit()
        assert GRUPO_TESTE not in repo.grupos_ativos(s, "material")
        ainda_existe = s.execute(text(
            "SELECT count(*) FROM grupo_permitido WHERE codigo = :c"),
            {"c": GRUPO_TESTE}).scalar_one()
    assert ainda_existe == 1


@pytestmark_db
def test_grupo_revogado_nao_mexe_no_escopo_da_pesquisa(banco_limpo, grupos_limpos):
    """Revogar grupo é recorte de DOWNLOAD. O escopo continua vindo de `pdm_permitido` — se
    isso se confundir, revogar um grupo derrubaria itens já curados do catálogo."""
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.permitir_grupo(s, "material", GRUPO_TESTE)
        repo.derivar_catalogo_item(s)
        s.commit()

        repo.revogar_grupo(s, "material", GRUPO_TESTE)
        repo.derivar_catalogo_item(s)
        s.commit()
        active = s.execute(text("SELECT active FROM catalogo_item WHERE codigo = :c"),
                          {"c": COD_MATERIAL}).scalar_one()
    assert active is True


@pytestmark_db
def test_listar_grupos_sem_filtro_de_tipo(grupos_limpos):
    """Mesma armadilha de `AmbiguousParameter` que `listar_permitidos` teve."""
    with db.session() as s:
        grupos = repo.listar_grupos(s)
    assert grupos, "o seed da 0006 deve aparecer aqui"
    assert {g["tipo"] for g in grupos} <= {"material", "servico"}


# ── Delta ────────────────────────────────────────────────────────────────────────────

def _banco_tem_catalogo_real() -> bool:
    """`delta_catalogo` captura um snapshot do catálogo INTEIRO — rodá-lo contra um banco com
    acervo real inseriria milhares de linhas em `catalogo_snapshot` e mexeria no baseline do
    delta do usuário. O teste só roda em banco sem catálogo (descartável ou recém-migrado)."""
    if not db.is_available()[0]:
        return True
    with db.session() as s:
        return s.execute(text(
            "SELECT count(*) FROM catalogo_item WHERE codigo <> ALL(:v)"),
            {"v": [COD_MATERIAL, COD_SERVICO]}).scalar_one() > 0


@pytestmark_db
@pytest.mark.skipif(_banco_tem_catalogo_real(),
                    reason="banco tem catálogo real — o snapshot do delta alteraria o baseline")
def test_delta_trata_a_primeira_execucao_como_baseline(banco_limpo):
    """A armadilha registrada na step: sem snapshot anterior, marcar tudo como 'novo' faria
    a primeira execução reportar o catálogo inteiro como novidade. Delta zero é o correto."""
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.derivar_catalogo_item(s)
        s.execute(text("DELETE FROM catalogo_snapshot"))
        s.commit()

        primeiro = repo.delta_catalogo(s)
        s.commit()
    assert primeiro["baseline"] == 1
    assert primeiro["codigos_novos"] == 0 and primeiro["codigos_removidos"] == 0


@pytestmark_db
@pytest.mark.skipif(_banco_tem_catalogo_real(),
                    reason="banco tem catálogo real — o snapshot do delta alteraria o baseline")
def test_delta_acusa_codigo_novo_na_segunda_execucao(banco_limpo):
    with db.session() as s:
        _semear_raw(s)
        repo.permitir(s, "material", PDM_TESTE)
        repo.derivar_catalogo_item(s)
        s.execute(text("DELETE FROM catalogo_snapshot"))
        s.commit()
        repo.delta_catalogo(s)          # baseline com só o material
        s.commit()

        repo.permitir(s, "servico", COD_SERVICO)   # serviço entra no escopo agora
        repo.derivar_catalogo_item(s)
        segundo = repo.delta_catalogo(s)
        s.commit()
    assert segundo["baseline"] == 0
    assert segundo["codigos_novos"] == 1, "o serviço recém-curado deve aparecer como novo"
