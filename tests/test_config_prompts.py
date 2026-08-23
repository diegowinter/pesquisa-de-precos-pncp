"""
Guarda da Fase 6 (docs/04_FASES.md): resolução de prompt (banco vs. fallback hardcoded) e o
CRUD versionado de config/prompt. `prompts_resolver.resolver` é puro e roda sempre; o resto
precisa de Postgres com o schema aplicado e é PULADO sem ele (mesmo padrão de test_runner.py).
"""

import pytest

from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)

_NOMES_TESTE = ("teste_fase6_prompt", "teste_fase6_ativar", "teste_fase6_e2e")


@pytest.fixture(autouse=True)
def _prompts_de_teste_limpos():
    """`prompt.name` é PK — rodar a suíte duas vezes reaproveitaria a mesma linha e acumularia
    versões (`proxima_versao_prompt` nunca reseta). Limpa antes E depois; `ON DELETE CASCADE`
    em `prompt_version` cuida do histórico junto."""
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text("DELETE FROM prompt WHERE name = ANY(:nomes)"),
                           {"nomes": list(_NOMES_TESTE)})
    yield
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text("DELETE FROM prompt WHERE name = ANY(:nomes)"),
                           {"nomes": list(_NOMES_TESTE)})


# ── prompts_resolver (puro, sem banco) ───────────────────────────────────────────────

def test_resolver_sem_ativos_cai_no_fallback():
    texto, versao_id = prompts_resolver.resolver(None, "classificar_item", "TEXTO FALLBACK")
    assert texto == "TEXTO FALLBACK"
    assert versao_id is None


def test_resolver_com_ativo_usa_o_template_do_banco():
    ativos = {"comparar_par": ("cat={texto_catalogo} item={texto_item}", 42)}
    texto, versao_id = prompts_resolver.resolver(
        ativos, "comparar_par", "FALLBACK", texto_catalogo="X", texto_item="Y")
    assert texto == "cat=X item=Y"
    assert versao_id == 42


def test_resolver_ignora_ativo_de_outro_prompt():
    ativos = {"comparar_par": ("...", 1)}
    texto, versao_id = prompts_resolver.resolver(ativos, "classificar_item", "FALLBACK")
    assert texto == "FALLBACK"
    assert versao_id is None


def test_resolver_template_mal_formado_cai_no_fallback():
    ativos = {"comparar_par": ("falta {placeholder_inexistente}", 1)}
    texto, versao_id = prompts_resolver.resolver(ativos, "comparar_par", "FALLBACK",
                                                  texto_catalogo="X", texto_item="Y")
    assert texto == "FALLBACK"
    assert versao_id is None


# ── config_version/config_value: imutabilidade e diff (ADR-014) ──────────────────────

@pytestmark_db
def test_diff_config_reporta_so_o_que_mudou():
    with db.session() as sessao:
        a = repo.criar_config_versao(sessao, "teste-fase6-a")
        repo.gravar_config(sessao, a, {"top_n": 0, "min_itens": 1})
        b = repo.criar_config_versao(sessao, "teste-fase6-b")
        repo.gravar_config(sessao, b, {"top_n": 5, "min_itens": 1})
        diff = repo.diff_config(sessao, a, b)
    assert diff["diferencas"] == [{"key": "top_n", "de": 0, "para": 5}]


@pytestmark_db
def test_config_versao_e_imutavel_editar_cria_nova():
    with db.session() as sessao:
        v1 = repo.criar_config_versao(sessao, "teste-fase6-imut")
        repo.gravar_config(sessao, v1, {"top_n": 0})
        v2 = repo.criar_config_versao(sessao, "teste-fase6-imut")
        repo.gravar_config(sessao, v2, {"top_n": 5})
    assert v1 != v2
    with db.session() as sessao:
        assert repo.ler_config(sessao, v1) == {"top_n": 0}
        assert repo.ler_config(sessao, v2) == {"top_n": 5}


# ── prompt/prompt_version: histórico e activeção ───────────────────────────────────────

@pytestmark_db
def test_criar_versao_prompt_nasce_inativa():
    with db.session() as sessao:
        repo.upsert_prompt(sessao, "teste_fase6_prompt", "prompt de teste", "chat",
                           "template v1", version=1, active=True)
        v2_id = repo.criar_prompt_versao(sessao, "teste_fase6_prompt", "template v2")
        versoes = repo.prompt_versoes(sessao, "teste_fase6_prompt")
    por_id = {v["id"]: v for v in versoes}
    assert por_id[v2_id]["active"] is False


@pytestmark_db
def test_ativar_prompt_versao_desativa_a_anterior():
    with db.session() as sessao:
        repo.upsert_prompt(sessao, "teste_fase6_ativar", "prompt de teste", "chat",
                           "template v1", version=1, active=True)
        repo.criar_prompt_versao(sessao, "teste_fase6_ativar", "template v2")
        assert repo.ativar_prompt_versao(sessao, "teste_fase6_ativar", 2)
        versoes = {v["version"]: v["active"] for v in
                  repo.prompt_versoes(sessao, "teste_fase6_ativar")}
    assert versoes == {1: False, 2: True}


@pytestmark_db
def test_template_prompt_ativo_alimenta_o_resolver_fim_a_fim():
    with db.session() as sessao:
        repo.upsert_prompt(sessao, "teste_fase6_e2e", "prompt de teste", "chat",
                           "olá {saudado}", version=1, active=True)
        ativos = prompts_resolver.carregar_ativos(sessao, ["teste_fase6_e2e"])
    texto, versao_id = prompts_resolver.resolver(ativos, "teste_fase6_e2e", "FALLBACK",
                                                  saudado="mundo")
    assert texto == "olá mundo"
    assert versao_id is not None
