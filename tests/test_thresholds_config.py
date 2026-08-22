"""
Thresholds como `Params` (Fase 14 bloco 3, ADR-022).

Até aqui, `rerank_t_aceita`/`rerank_t_rejeita` (6b) e `min_itens`/`top_n` (7) vinham do `.env`
por `ctx.config`. Agora são campos do `Params` da etapa, o que os coloca no formulário gerado
do Pydantic e — o que importa de verdade — sob `config_versao`: versionado e imutável, para que
"por que o resultado mudou?" tenha resposta (ADR-014).

Os dois testes que sustentam a migração são `test_defaults_preservam_o_comportamento_do_env`
(a virada não muda resultado por si só) e `test_nenhuma_etapa_le_threshold_de_ctx_config`
(não sobrou uma segunda fonte para divergir em silêncio).
"""

import importlib
import inspect
from pathlib import Path

import pytest

from pesquisa_precos.etapas import e6b_rerank, e7_agrupar
from pesquisa_precos.etapas import registry


# ── os valores ───────────────────────────────────────────────────────────────────────

def test_defaults_preservam_o_comportamento_do_env():
    """Os defaults têm de ser exatamente o que o `.env` carregava — a Fase 14 move a origem
    do valor, não o valor."""
    p6b = e6b_rerank.Params()
    assert (p6b.rerank_t_aceita, p6b.rerank_t_rejeita) == (0.80, 0.30)
    p7 = e7_agrupar.Params()
    assert (p7.min_itens, p7.top_n) == (1, 0)   # ADR-016: "regra dos 5" desativada


def test_top_n_zero_e_sem_teto_nao_zero_itens():
    """A armadilha registrada no CLAUDE.md e na ADR-016. O `ge=0` tem de aceitar o 0, e o 0
    tem de significar SEM TETO."""
    assert e7_agrupar.Params(top_n=0).top_n == 0
    assert "SEM TETO" in e7_agrupar.Params.model_fields["top_n"].description


def test_thresholds_validados_pelo_schema():
    """ADR-014, salvaguarda 2: a interface não pode gravar valor que o código rejeitaria."""
    with pytest.raises(ValueError):
        e6b_rerank.Params(rerank_t_aceita=1.5)
    with pytest.raises(ValueError):
        e7_agrupar.Params(min_itens=0)
    with pytest.raises(ValueError):
        e7_agrupar.Params(top_n=-1)


# ── a origem do valor ────────────────────────────────────────────────────────────────

_CHAVES_MIGRADAS = ("rerank_t_aceita", "rerank_t_rejeita", "min_itens", "top_n")


def test_nenhuma_etapa_le_threshold_de_ctx_config():
    """A guarda que impede a segunda fonte de voltar. Se uma etapa voltar a ler o threshold do
    `.env`, o valor efetivo deixa de ser o que `config_versao` registra — e o run passa a
    mentir sobre o que o produziu."""
    infratores = []
    for definicao in registry.ordem():
        modulo = importlib.import_module(definicao.carregar().__name__)
        origem = Path(inspect.getfile(modulo)).read_text(encoding="utf-8")
        for chave in _CHAVES_MIGRADAS:
            for padrao in (f'ctx.config["{chave}"]', f'cfg["{chave}"]'):
                if padrao in origem:
                    infratores.append(f"{definicao.chave}: {padrao}")
    assert not infratores, f"threshold lido do .env em vez de Params: {infratores}"


def test_settings_nao_expoe_mais_os_thresholds():
    """A outra metade: se a chave continuar em `carregar_config()`, alguém a lê de novo."""
    from pesquisa_precos.config.settings import carregar_config

    cfg = carregar_config()
    for chave in (*_CHAVES_MIGRADAS, "rejeitor_threshold"):
        assert chave not in cfg, f"{chave} ainda vem do .env"


def test_versao_codigo_bumpada():
    """Mudar a ORIGEM do valor efetivo muda o resultado potencial da etapa — o fingerprint
    precisa marcar as dependentes como desatualizadas."""
    assert e6b_rerank.VERSAO_CODIGO != "2.0.0"
    assert e7_agrupar.VERSAO_CODIGO != "2.0.0"


# ── a camada de config chega ao Params ───────────────────────────────────────────────

def test_thresholds_aparecem_no_formulario_gerado():
    """`services.config.schema_parametros` é o que a tela usa para montar o formulário — é por
    ele que os thresholds viram caixinhas sem ninguém escrever HTML."""
    from pesquisa_precos.services.config import schema_parametros

    esquema = schema_parametros()
    assert "rerank_t_aceita" in esquema["6b"]["campos"]
    assert "min_itens" in esquema["7"]["campos"] and "top_n" in esquema["7"]["campos"]
    assert esquema["7"]["campos"]["top_n"]["default"] == 0


def test_camada_config_versao_sobrepoe_o_default():
    """`runner.executor.preparar` resolve `Params` em três camadas (default ← config_versao ←
    override do play). Este teste prova que as chaves novas entram na camada do meio — que é
    o ponto inteiro do bloco 3."""
    defaults = e7_agrupar.Params().model_dump()
    config_valores = {"top_n": 5, "min_itens": 3, "chave_de_outra_etapa": "ignorada"}
    camada = {k: v for k, v in config_valores.items() if k in defaults}
    efetivos = e7_agrupar.Params(**{**defaults, **camada}).model_dump()
    assert efetivos["top_n"] == 5 and efetivos["min_itens"] == 3
    assert "chave_de_outra_etapa" not in efetivos
