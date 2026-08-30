"""
Guarda das Fases 10 (bloco D) e 11: etapas 5, 6a-6c e 8 no banco, com cômputo externalizável.

O que estes testes protegem:
  1. o XLSX ir para `export.conteudo` e voltar íntegro — é o que substitui o arquivo em disco
     (ADR-018 §2), e um export corrompido só apareceria na mão do usuário final;
  2. a estratégia `visao` receber IMAGENS em vez de uma pasta de PDFs — a mudança que permite
     tirar o PyMuPDF do container sem quebrar a rota de exceção (ADR-019);
  3. as capabilities `pdf`/`pareamento` exigirem um serviço configurado — desde a ADR-021 não
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


# ── Capacidades exigem provider cadastrado (Fase 11 + Fase 14) ───────────────────────
#
# Estes testes protegiam a ADR-021 pelo `.env` (`PDF_BASE_URL` vazio → falha). A ADR-022 tirou
# o `.env` da resolução, então a MESMA invariante é verificada pelo banco: sem provider
# apontado, ou com provider sem endereço, a step para antes de começar. O que não pode
# acontecer, em nenhum dos dois mundos, é cair num caminho em processo.

@pytest.fixture
def provedor_de_teste():
    """Escreve direto no repo (não pelo service) porque alguns casos precisam de uma linha que
    o service RECUSA gravar — é o cenário "alguém editou a tabela na mão"."""
    from pesquisa_precos.db.repos import execution as repo

    criados: list[str] = []
    # Ver `_snapshot_capabilities` em test_provedores_crud.py: apontar capability REAL para um
    # provider de teste e depois apagar deixaria a instalação sem configuração.
    from sqlalchemy import text as _text
    with db.session() as sessao:
        antes = [dict(r) for r in sessao.execute(_text(
            "SELECT capability, provider, model, fallback FROM provider_capability"
        )).mappings()]

    def criar(name: str, capabilities: list[str], base_url: str):
        with db.session() as sessao:
            repo.upsert_provedor(sessao, name, capabilities, base_url)
            for c in capabilities:
                repo.apontar_capacidade(sessao, c, name)
        criados.append(name)

    yield criar

    from sqlalchemy import text

    with db.session() as sessao:
        for name in criados:
            sessao.execute(text("DELETE FROM provider_capability WHERE provider = :n"),
                           {"n": name})
            sessao.execute(text("DELETE FROM provider_status WHERE provider = :n"), {"n": name})
            sessao.execute(text("DELETE FROM provider WHERE name = :n"), {"n": name})
        for linha in antes:
            repo.apontar_capacidade(sessao, linha["capability"], linha["provider"],
                                    linha["model"], linha["fallback"])


@pytestmark_db
@pytest.mark.parametrize("capability", ["extract", "matching", "rerank", "embed", "chat"])
def test_capacidade_sem_provedor_falha_em_vez_de_rodar_aqui(capability):
    """ADR-021 + ADR-022: não existe adapter em processo NEM fallback para o `.env`. Sem
    provider apontado, a step PARA com uma message que diz o que configurar — em vez de
    carregar torch/PyMuPDF no processo que só deveria baixar e gravar no banco."""
    from sqlalchemy import text

    from pesquisa_precos.providers.resolver import (
        CapabilityNotConfigured,
        Providers,
        resolver_capacidade,
    )

    with db.session() as sessao:
        # garante a ausência de apontamento, sem tocar no que o operador configurou de verdade
        existente = sessao.execute(
            text("SELECT provider FROM provider_capability "
                 "WHERE capability = CAST(:c AS capability)"), {"c": capability}).first()
        if existente:
            pytest.skip(f"`{capability}` está configurada nesta instalação ({existente[0]})")
        with pytest.raises(CapabilityNotConfigured) as exc:
            resolver_capacidade(capability, sessao=sessao)
    assert capability in str(exc.value) and "/providers" in str(exc.value)
    with pytest.raises(CapabilityNotConfigured):
        assert getattr(Providers(), capability)


@pytestmark_db
def test_capabilities_viram_remotas_com_provedor_apontado(provedor_de_teste):
    from pesquisa_precos.providers.resolver import Providers

    provedor_de_teste("teste-d-par", ["matching"], "http://gpu:8300")
    assert type(Providers().matching).__name__ == "PareamentoRemotoAdapter"


@pytestmark_db
def test_extract_e_um_llm_e_nao_um_servico_do_companion(provedor_de_teste):
    """ADR-023: `extract` deixou de ser o serviço `pdf` e virou um provedor de chat multimodal.

    Consequências que este teste fixa: vira um `ChatAdapter`, e NÃO passa por
    `_exigir_servico` — que exigiria `/health`, endpoint que nenhum provedor de LLM expõe.
    """
    from pesquisa_precos.db.repos import execution as repo
    from pesquisa_precos.providers.resolver import Providers, criar_extract

    provedor_de_teste("teste-d-extract", ["extract"], "https://exemplo.invalido/v1")
    # O `Curador` monta o cliente OpenAI já na construção, e ele exige uma chave. Nenhuma
    # chamada de rede acontece aqui — só o empacotamento do adapter.
    with db.session() as sessao:
        repo.gravar_api_key(sessao, "teste-d-extract", "chave-de-teste")
        sessao.commit()
        assert type(criar_extract(sessao=sessao)).__name__ == "ChatAdapter"
    assert type(Providers().extract).__name__ == "ChatAdapter"


@pytestmark_db
def test_provedor_sem_base_url_reprova(provedor_de_teste):
    """Linha gravada na mão, com `base_url` vazia: `_exigir_servico` tem de recusar. Vazio
    NUNCA volta a significar "roda aqui" (ADR-021)."""
    from pesquisa_precos.providers.resolver import CapabilityNotConfigured, Providers

    provedor_de_teste("teste-d-vazio", ["matching"], "")
    with pytest.raises(CapabilityNotConfigured, match="base_url"):
        assert Providers().matching


@pytestmark_db
def test_health_check_reprova_capacidade_sem_provedor():
    """Sem provider a step não pode começar, e o health check pré-play é onde isso aparece —
    antes de gastar. Tem de virar linha vermelha, não exceção: é o painel de diagnóstico."""
    from sqlalchemy import text

    from pesquisa_precos.providers import health

    with db.session() as sessao:
        if sessao.execute(text("SELECT 1 FROM provider_capability "
                               "WHERE capability = 'extract'")).first():
            pytest.skip("`extract` está configurada nesta instalação")
    resultado = health.checar_capacidade("extract")
    assert resultado["healthy"] is False
    assert resultado["source"] == "não configurado"


def test_nenhum_adapter_em_processo_sobreviveu():
    """Guarda da ADR-021: um adapter "em processo" reintroduzido traria torch/PyMuPDF de volta
    para o processo que orquestra, e faria isso silenciosamente — só a conta de memória do
    servidor acusaria."""
    import inspect

    from pesquisa_precos.providers import adaptadores

    nomes = [n for n, o in vars(adaptadores).items()
             if inspect.isclass(o) and n.endswith("Adapter")]
    assert not [n for n in nomes if "Processo" in n], nomes


def test_health_check_reprova_capacidade_sem_servico():
    """Sem `base_url` a step não pode começar: o health check pré-play é onde isso aparece,
    antes de gastar. Era o inverso enquanto existia caminho em processo.

    A checagem é sobre as funções puras de sondagem, NÃO sobre uma capacidade nomeada: a
    versão anterior afirmava que `pdf` reprova — o que era verdade só enquanto ninguém tivesse
    cadastrado um provedor. Desde a ADR-022 não há mais fallback para o `.env`, e no dia em
    que um provedor foi cadastrado o teste passou a falhar contra o banco real do usuário,
    sem nada ter quebrado.
    """
    from pesquisa_precos.providers import health

    assert health.sondar_url("")["healthy"] is False
    assert health.sondar_health("http://127.0.0.1:1")["healthy"] is False


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
        # Run próprio e descartável. Antes o teste chamava `run_aberto_ou_criar`, que
        # REAPROVEITA o último run aberto de mesmo rótulo — e por isso deixava a run
        # "teste_bloco_d" viva no banco depois de passar. Teste que toca tabela de domínio
        # limpa o que criou (ver CLAUDE.md).
        cv = repo_exec.config_versao_por_rotulo(s, "default")
        assert cv is not None, "sem config_version 'default' no banco"
        run_id = repo_exec.criar_run(s, "teste_bloco_d", cv)
        export_id = repo_grupo.registrar_export(
            s, run_id, "completo", None, 1, 1, "hash-de-teste",
            conteudo=conteudo, nome_arquivo="itens_plaseg.xlsx")
        s.commit()
        try:
            name, bytes_de_volta = repo_grupo.conteudo_export(s, export_id)
            assert name == "itens_plaseg.xlsx"
            assert bytes_de_volta == conteudo, "o XLSX tem que voltar byte a byte"
            caminho = s.execute(text("SELECT arquivo FROM export WHERE id = :i"),
                                {"i": export_id}).scalar_one()
            assert caminho is None, "no caminho banco não existe arquivo em disco"
        finally:
            s.execute(text("DELETE FROM export WHERE id = :i"), {"i": export_id})
            s.execute(text("DELETE FROM run WHERE id = :r"), {"r": run_id})
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
        raise AssertionError(f"step {key}: `estimar` leu arquivo em vez de consultar o banco")

    monkeypatch.setattr(pd, "read_csv", proibido)
    monkeypatch.setattr(pd, "read_parquet", proibido)

    # `config` real (e não `{}`): a 6c consulta `model_pass1` para montar a estimativa de custo.
    ctx = NullContext(key)
    modulo = registry.obter(key).carregar()
    assert "fonte" not in modulo.Params.model_fields, "o campo `fonte` saiu na Fase 13"
    modulo.estimate(modulo.Params(), ctx)
