"""
Guarda da Fase 0: a árvore nova aponta para os MESMOS arquivos da árvore antiga.

O risco específico desta fase não é "quebrou e deu erro" — é "moveu, rodou, e escreveu num
`data/` diferente". Isso não levanta exceção: a etapa simplesmente não encontra a entrada
resumível, reprocessa tudo do zero e cobra o LLM de novo. Estes testes existem para que esse
modo de falha apareça em segundos, não numa fatura.

Não testam regra de negócio (isso é a Fase 9) — testam fiação.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest

from pesquisa_precos.config import paths

ETAPAS = [
    "e0a_catalogo", "e1_termos", "e2_coletar", "e3_classificar", "e4_cortar",
    "e5a_ocr", "e5b_extrair", "e5_alt_a_tabela", "e5_alt_b_casar",
    "e6a_pares", "e6b_rerank", "e6c_validar", "e7_agrupar", "e8_exportar",
]


def test_raiz_e_a_do_repositorio():
    """`paths.RAIZ` tem que ser a pasta que contém `pyproject.toml`, não a do módulo."""
    assert (paths.RAIZ / "pyproject.toml").is_file()
    assert paths.DATA.name == "data"


@pytest.mark.parametrize("nome", ETAPAS)
def test_etapa_importa(nome):
    importlib.import_module(f"pesquisa_precos.etapas.{nome}")


def test_todo_o_pacote_importa():
    """Nenhum import quebrado sobrou da movimentação (inclui core/ e providers/)."""
    import pesquisa_precos

    for m in pkgutil.walk_packages(pesquisa_precos.__path__, "pesquisa_precos."):
        importlib.import_module(m.name)


@pytest.mark.parametrize("nome", ETAPAS)
def test_caminhos_da_etapa_ficam_dentro_de_data(nome):
    """
    Toda constante de caminho de uma etapa tem que cair dentro de `paths.DATA`.

    Se um `DATA = Path(__file__).parent / "data"` tivesse sobrevivido à movimentação, ele
    apontaria para `pesquisa_precos/etapas/data/` — dentro do pacote, fora do acervo.
    """
    mod = importlib.import_module(f"pesquisa_precos.etapas.{nome}")
    achou = False
    for atributo in dir(mod):
        if atributo.startswith("_"):
            continue
        valor = getattr(mod, atributo)
        if not isinstance(valor, Path):
            continue
        achou = True
        assert paths.DATA in valor.parents or valor == paths.DATA, (
            f"{nome}.{atributo} = {valor} está fora de {paths.DATA}"
        )
    assert achou, f"{nome} não expõe nenhuma constante de caminho — a movimentação as perdeu?"


def test_nao_restou_nenhum_import_do_pacote_scripts():
    """`scripts/` deixou de existir; um import residual só falharia em runtime."""
    for arq in paths.RAIZ.rglob("*.py"):
        if ".venv" in arq.parts or "__pycache__" in arq.parts or arq == Path(__file__):
            continue
        texto = arq.read_text(encoding="utf-8")
        assert "from scripts" not in texto and "import scripts" not in texto, arq
