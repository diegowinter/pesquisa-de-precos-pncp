"""
Guarda da Fase 0: a árvore nova aponta para os MESMOS arquivos da árvore antiga.

O risco específico desta fase não é "quebrou e deu erro" — é "moveu, rodou, e escreveu num
`data/` diferente". Isso não levanta exceção: a step simplesmente não encontra a entrada
resumível, reprocessa tudo do zero e cobra o LLM de novo. Estes testes existem para que esse
mode de falha apareça em segundos, não numa fatura.

Não testam regra de negócio (isso é a Fase 9) — testam fiação.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest

from pesquisa_precos.config import paths

ETAPAS = [
    "e0a_catalogo", "e1_termos", "e2_collect", "e3_classify", "e4_cut",
    "e5_extract",
    "e6a_pairs", "e6b_rerank", "e6c_validate", "e7_group", "e8_export",
]


def test_raiz_e_a_do_repositorio():
    """`paths.RAIZ` tem que ser a pasta que contém `pyproject.toml`, não a do módulo."""
    assert (paths.RAIZ / "pyproject.toml").is_file()
    assert paths.DATA.name == "data"


@pytest.mark.parametrize("name", ETAPAS)
def test_etapa_importa(name):
    importlib.import_module(f"pesquisa_precos.steps.{name}")


def test_todo_o_pacote_importa():
    """Nenhum import quebrado sobrou da movimentação (inclui core/ e providers/)."""
    import pesquisa_precos

    for m in pkgutil.walk_packages(pesquisa_precos.__path__, "pesquisa_precos."):
        importlib.import_module(m.name)


@pytest.mark.parametrize("name", ETAPAS)
def test_etapa_nao_expoe_nenhum_caminho(name):
    """
    Fase 13 (ADR-020): nenhuma step pode ter constante de caminho.

    Este teste era o INVERSO — "todo caminho da step cai dentro de `paths.DATA`" — enquanto
    as etapas escreviam CSV. Com o banco como único meio de persistência, qualquer `Path` de
    volta ao módulo de uma step é o começo do caminho paralelo de novo: um arquivo que a web
    não sabe servir, que o container não persiste e que ninguém lembra de migrar.

    `paths.py` continua existindo, mas é do importador (`migracao/`), não das etapas.
    """
    mod = importlib.import_module(f"pesquisa_precos.steps.{name}")
    caminhos = {a: getattr(mod, a) for a in dir(mod)
                if not a.startswith("_") and isinstance(getattr(mod, a), Path)}
    assert not caminhos, f"{name} voltou a expor caminho(s): {caminhos}"


@pytest.mark.parametrize("name", ETAPAS)
def test_etapa_nao_importa_paths(name):
    """A outra metade da mesma regra: nem por importação indireta."""
    source = (paths.RAIZ / "pesquisa_precos" / "steps" / f"{name}.py").read_text(encoding="utf-8")
    assert "config import paths" not in source and "config.paths" not in source, (
        f"{name} importa `config.paths` — ver o docstring de `paths.py`")


def test_nao_restou_nenhum_import_do_pacote_scripts():
    """`scripts/` deixou de existir; um import residual só falharia em runtime."""
    for arq in paths.RAIZ.rglob("*.py"):
        if ".venv" in arq.parts or "__pycache__" in arq.parts or arq == Path(__file__):
            continue
        texto = arq.read_text(encoding="utf-8")
        assert "from scripts" not in texto and "import scripts" not in texto, arq


# ── Fase 14 (ADR-022): o `.env` deixou de ser fonte de configuração ─────────────────
#
# A guarda que impede o caminho removido de voltar. Ele não voltaria por decisão consciente —
# voltaria como um `os.getenv("GPU_BASE_URL")` conveniente dentro de uma step, e a partir daí
# a tela de provedores passaria a mentir sobre o que está em uso.

_VARS_QUE_MIGRARAM = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_PASS1", "OPENAI_MODEL_PASS2",
    "LOCAL_BASE_URL", "LOCAL_MODEL", "LOCAL_API_KEY", "GPU_BASE_URL", "GPU_API_KEY",
    "PDF_BASE_URL", "PDF_API_KEY", "PAREAMENTO_BASE_URL", "PAREAMENTO_API_KEY",
    "EMBEDDER_MODEL", "RERANKER_MODEL", "REJEITOR_THRESHOLD", "RERANK_T_ACEITA",
    "RERANK_T_REJEITA", "MIN_ITENS", "TOP_N",
)

# `tools/seed_providers.py` é a ÚNICA exceção legítima: a ponte de mão única que lê o
# `.env` antigo uma vez para popular o banco.
_PODEM_LER_O_ENV_ANTIGO = ("seed_providers.py",)


def _modulos_do_pacote():
    import pesquisa_precos

    raiz = Path(pesquisa_precos.__file__).parent
    return [arq for arq in raiz.rglob("*.py") if "__pycache__" not in str(arq)]


@pytest.mark.parametrize("variavel", _VARS_QUE_MIGRARAM)
def test_nenhum_modulo_le_variavel_que_migrou_para_o_banco(variavel):
    """ADR-022: quem sabe model/URL/key/threshold é o banco, e o único ponto que o lê é
    `providers/resolver.py`."""
    infratores = [arq.name for arq in _modulos_do_pacote()
                  if arq.name not in _PODEM_LER_O_ENV_ANTIGO
                  and f'"{variavel}"' in arq.read_text(encoding="utf-8")]
    assert not infratores, f"{variavel} lida em {infratores} — deveria vir do banco"


def test_resolver_nao_tem_mais_caminho_env():
    """A função `_resolver_via_env` saiu na ADR-022. Um fallback reintroduzido devolveria o
    mode em que um erro de configuração vira step rodando com o model errado."""
    from pesquisa_precos.providers import resolver

    assert not hasattr(resolver, "_resolver_via_env")
    source = Path(resolver.__file__).read_text(encoding="utf-8")
    assert 'source="env"' not in source


def test_settings_nao_resolve_mais_provedor():
    """`resolver_provedor`/`exigir`/`custo_por_chamada` eram a API de provider do `.env`."""
    from pesquisa_precos.config import settings

    for funcao in ("resolver_provedor", "exigir", "custo_por_chamada"):
        assert not hasattr(settings, funcao), f"settings.{funcao} deveria ter saído (ADR-022)"


def test_curador_nao_tem_mais_from_provedor():
    """Era o segundo caminho da 6c: montava o cliente pelo `.env`, contornando o resolver."""
    from pesquisa_precos.providers.llm_curador import Curador

    assert not hasattr(Curador, "from_provedor")


def test_template_nao_usa_variavel_que_a_rota_nao_fornece():
    """Variável indefinida no Jinja não levanta: renderiza vazio, em silêncio.

    Foi assim que `{% for c in capacidades %}` sobreviveu ao rename para `capabilities`
    (2026-08-25): a tela `/providers` desenhava ZERO checkboxes de capacidade e o formulário
    reprovava com "selecione ao menos uma capability" sem ter o que selecionar. O mesmo
    aconteceu com `aberto`/`open` em `prompts.html`.

    A checagem é frouxa de propósito — basta o nome aparecer em `app.py`, não importa como.
    Ela não prova que a rota certa fornece a variável certa; só pega o nome órfão.
    """
    import re

    from jinja2 import Environment, FileSystemLoader, meta

    import pesquisa_precos.web.app as web_app

    raiz = Path(__file__).resolve().parents[1]
    pasta = raiz / "pesquisa_precos" / "web" / "templates"
    fonte_app = (raiz / "pesquisa_precos" / "web" / "app.py").read_text(encoding="utf-8")
    conhecidas = (set(re.findall(r'"(\w+)"', fonte_app))
                  | set(re.findall(r"(\w+)=", fonte_app))
                  | set(web_app.templates.env.globals))

    env = Environment(loader=FileSystemLoader(str(pasta)))
    orfas = {}
    for arquivo in sorted(pasta.glob("*.html")):
        usadas = meta.find_undeclared_variables(
            env.parse(arquivo.read_text(encoding="utf-8")))
        faltando = sorted(nome for nome in usadas if nome not in conhecidas)
        if faltando:
            orfas[arquivo.name] = faltando
    assert not orfas, f"variáveis que nenhuma rota fornece: {orfas}"


def test_nenhum_codigo_inalcancavel_depois_de_return():
    """Código depois de `return`/`raise` no mesmo bloco é sempre erro, e o `ruff` não o vê.

    Em 2026-08-29 a etapa 5 tinha 50 linhas — a extração inteira — indentadas para dentro do
    `if not linhas_paginas:`, logo depois do `return` dele. O caminho normal caía no fim da
    função e devolvia `None`; quem chamava fazia `linhas_item, linhas_doc = resultado` e
    estourava com "cannot unpack non-iterable NoneType". Foram 1.146 documentos perdidos, cada
    um com o download do PNCP e o upload ao serviço de PDF já pagos.

    Sintaticamente é código válido, e por isso nenhuma ferramenta reclamou.
    """
    import ast

    raiz = Path(__file__).resolve().parents[1]
    achados = []
    for arquivo in (raiz / "pesquisa_precos").rglob("*.py"):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), str(arquivo))
        for no in ast.walk(arvore):
            corpo = getattr(no, "body", None)
            if not isinstance(corpo, list):
                continue
            for i, cmd in enumerate(corpo[:-1]):
                if isinstance(cmd, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    achados.append(
                        f"{arquivo.relative_to(raiz)}:{corpo[i + 1].lineno} "
                        f"depois de {type(cmd).__name__.lower()} na linha {cmd.lineno}")
    assert not achados, "código inalcançável:\n  " + "\n  ".join(achados)
