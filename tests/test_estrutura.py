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


# Vocabulário da abordagem de extração aposentada (ADR-023). Cada entrada é (agulha, o que
# ela denunciaria se voltasse). São palavras que NÃO aparecem em nenhum outro contexto do
# pacote — `vision` não entra na lista porque colide com "visão"/`input_modalities` em texto
# de comentário; o que a representa é `estrategia`, que era o eixo do desenho todo.
_RESQUICIOS_DA_EXTRACAO_ANTIGA = {
    "strategies": "o pacote de estratégias plugáveis",
    "janela_max": "a estratégia de janela de texto",
    "extrair_tabela_pdf": "a extração por imagem de página (visão)",
    "extrair_tabela_texto": "a extração por chunk de texto",
    "extrair_item_pdf": "a extração guiada de um item por vez",
    "documento_pagina": "a tabela de texto por página",
    "PdfRemotoAdapter": "o cliente do serviço `pdf` do companion",
    "escolher_estrategia": "o roteamento automático entre estratégias",
}


def _linhas_de_codigo(arquivo: Path) -> dict[int, str]:
    """Linhas do arquivo SEM comentários e SEM docstrings, indexadas por número.

    A distinção importa: explicar em prosa o que foi removido e por quê é o que mantém uma
    decisão rastreável — proibir a palavra até no comentário obrigaria a apagar a explicação
    junto com o código, que é o oposto do que se quer. O que não pode voltar é o CÓDIGO.

    Strings comuns continuam sendo varridas de propósito: é onde mora o SQL.
    """
    import ast
    import io
    import tokenize

    texto = arquivo.read_text(encoding="utf-8")
    linhas = dict(enumerate(texto.splitlines(), 1))

    arvore = ast.parse(texto, str(arquivo))
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = no.body[0] if no.body else None
        if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                and isinstance(doc.value.value, str)):
            for n in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                linhas.pop(n, None)

    for token in tokenize.generate_tokens(io.StringIO(texto).readline):
        if token.type == tokenize.COMMENT:
            n = token.start[0]
            if n in linhas:
                linhas[n] = linhas[n][: token.start[1]]
    return linhas


def test_nenhum_resquicio_da_extracao_por_estrategias():
    """A ADR-023 trocou a etapa 5 inteira, e o pedido foi explícito: sem sobra.

    Código morto de uma abordagem substituída é pior que código feio — quem for entender a
    etapa 5 depois vai encontrar dois desenhos e nenhuma pista de qual está vivo. O histórico
    do git é o resgate; a árvore fica com um caminho só.

    A varredura é sobre `pesquisa_precos/`, `migracao/` e `tools/` — não sobre `tests/`, onde
    citar o nome do que morreu (como aqui) é justamente o ponto.
    """
    raiz = Path(__file__).resolve().parents[1]
    achados = []
    for pacote in ("pesquisa_precos", "migracao", "tools"):
        for arquivo in (raiz / pacote).rglob("*.py"):
            for linha_n, linha in _linhas_de_codigo(arquivo).items():
                for agulha, o_que in _RESQUICIOS_DA_EXTRACAO_ANTIGA.items():
                    if agulha in linha:
                        achados.append(
                            f"{arquivo.relative_to(raiz)}:{linha_n} cita {agulha!r} "
                            f"({o_que})")
    assert not achados, (
        "resquício da extração por estratégias:\n  "
        + "\n  ".join(sorted(achados)))


def test_etapa_5_manda_o_documento_como_anexo():
    """O contrato da 1ª chamada: uma parte de texto + uma parte `file` com o PDF em base64.

    Se alguém trocar o anexo por texto extraído, a etapa volta a ser a que produziu zero item
    confirmado em 4.159 documentos — e volta em silêncio, porque o modelo responde do mesmo
    jeito quando não recebe documento nenhum.
    """
    from pesquisa_precos.providers.llm_curador import Curador

    class RespostaFake:
        content = "| item | preço |\n| --- | --- |\n| colete | 1.200,00 |"

    class LlmFake:
        def __init__(self):
            self.mensagens = None

        def invoke(self, mensagens):
            self.mensagens = mensagens
            return RespostaFake()

    curador = Curador.__new__(Curador)      # sem rede: só o empacotamento da mensagem
    curador._prompts_ativos = {}
    curador.llm = LlmFake()

    tabela = curador.extrair_tabela_documento(b"%PDF-1.7 conteudo", "ata.pdf")
    assert "colete" in tabela

    partes = curador.llm.mensagens[0].content
    tipos = [parte["type"] for parte in partes]
    assert tipos == ["text", "file"], f"a mensagem não leva o PDF anexo: {tipos}"
    anexo = partes[1]["file"]
    assert anexo["filename"] == "ata.pdf"
    assert anexo["file_data"].startswith("data:application/pdf;base64,")


def test_etapa_5_trata_sem_tabela_como_documento_sem_itens():
    """`SEM_TABELA` é a resposta combinada para "não há tabela de itens aqui". Precisa virar
    string vazia, e não uma descrição falsa gravada como se fosse a tabela do documento."""
    from pesquisa_precos.providers.llm_curador import Curador

    class RespostaFake:
        content = "SEM_TABELA"

    class LlmFake:
        def invoke(self, _mensagens):
            return RespostaFake()

    curador = Curador.__new__(Curador)
    curador._prompts_ativos = {}
    curador.llm = LlmFake()
    assert curador.extrair_tabela_documento(b"%PDF-1.7", "x.pdf") == ""


def test_chave_de_compra_e_derivada_num_lugar_so():
    """`urls.chave_compra` é a única função que separa a compra do sequencial da ata.

    Durante a investigação da ADR-024 o mesmo recorte foi escrito à mão em SQL como
    `regexp_replace(nc, '(/[0-9]{4})-[0-9]+$', '\\\\1')`. O shell comeu um backslash, o `\\1`
    virou `\\x01`, e a chave passou a sair SEM O ANO — produzindo contagens erradas sem
    levantar erro nenhum. Uma derivação escrita duas vezes é uma que diverge.

    A migration 0013 tem a sua própria cópia em SQL, de propósito: migration não importa o
    código da aplicação (ela precisa rodar contra um schema que o código já não descreve).
    Por isso `alembic/` fica fora da varredura.
    """
    import re

    raiz = Path(__file__).resolve().parents[1]
    padrao = re.compile(r"(left|substring|regexp_replace|split_part)\s*\(\s*[a-z_.]*"
                        r"numero_controle_pncp", re.IGNORECASE)
    achados = []
    for pacote in ("pesquisa_precos", "migracao", "tools"):
        for arquivo in (raiz / pacote).rglob("*.py"):
            for n, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
                if padrao.search(linha):
                    achados.append(f"{arquivo.relative_to(raiz)}:{n}  {linha.strip()[:70]}")
    # `models.py` declara a coluna GERADA do Postgres, que é a definição canônica em SQL —
    # o banco precisa dela para calcular `documento.compra_key` sozinho.
    achados = [a for a in achados if "db\\models.py" not in a and "db/models.py" not in a]
    assert not achados, ("derivação da chave de compra fora de `urls.chave_compra`:\n  "
                         + "\n  ".join(achados))


def test_item_nao_conhece_o_documento():
    """ADR-024: `item` não tem mais `numero_controle_pncp`.

    Enquanto tinha, cada linha de item nascia presa a UMA ata — e como a API do PNCP só
    entrega itens por compra, os 82 itens de um pregão viravam 82 linhas em cada uma das 25
    atas dele. Quem quer saber em qual documento o item foi achado pergunta a
    `item_enriquecido`, que é onde a etapa 5 grava essa descoberta.
    """
    from pesquisa_precos.db.models import Item, ItemEnriquecido

    colunas_item = {c.name for c in Item.__table__.columns}
    assert "numero_controle_pncp" not in colunas_item, \
        "`item` voltou a conhecer o documento — a duplicação volta junto"
    assert "compra_key" in colunas_item

    pk = {c.name for c in ItemEnriquecido.__table__.primary_key.columns}
    assert pk == {"item_key", "numero_controle_pncp"}, \
        f"a PK de item_enriquecido precisa carregar o documento (é {pk})"


def test_casamento_da_etapa_5_e_por_documento_e_nao_por_item():
    """O casamento manda os candidatos JUNTOS, numa chamada por documento (ADR-024).

    Voltar a perguntar item a item significa, para o pregão 507 da Embrapa, 82 perguntas em
    cada uma das 25 atas — 2.050 chamadas para 82 respostas úteis, e 71% delas impossíveis de
    responder com "sim" por construção.
    """
    from pesquisa_precos.providers.llm_curador import Curador

    assert hasattr(Curador, "casar_itens_tabela")
    assert not hasattr(Curador, "casar_item_tabela"), \
        "a versão por item voltou — ver ADR-024"

    fonte = (Path(__file__).resolve().parents[1]
             / "pesquisa_precos" / "steps" / "e5_extract.py").read_text(encoding="utf-8")
    assert "casar_itens_tabela" in fonte


def test_ninguem_junta_item_enriquecido_pela_item_key_sozinha():
    """`item_enriquecido` tem uma linha por DOCUMENTO desde a ADR-024 (média 3,47 por item,
    máximo 47). Juntar por `item_key` sozinho multiplica silenciosamente: a etapa 6b pontuou
    1.707 pares como se fossem 4.770, e as etapas 7/8 transformariam o mesmo item em várias
    referências de preço — a duplicação que a ADR-024 existe para eliminar.

    Quem precisa de UMA linha por item usa a view `item_enriquecido_melhor` (migration 0017),
    que além de deduplicar escolhe deterministicamente a melhor linha.
    """
    import pathlib
    import re

    ofensores = []
    for arquivo in list(pathlib.Path("pesquisa_precos").rglob("*.py")):
        texto = arquivo.read_text(encoding="utf-8")
        for linha in texto.splitlines():
            if re.search(r"JOIN\s+item_enriquecido\b", linha):
                ofensores.append(f"{arquivo}: {linha.strip()}")
    assert not ofensores, (
        "JOIN direto em `item_enriquecido` multiplica por documento — use "
        "`item_enriquecido_melhor`:\n" + "\n".join(ofensores))
