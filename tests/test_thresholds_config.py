"""
Thresholds como `Params` (Fase 14 bloco 3, ADR-022).

Até aqui, `rerank_t_aceita`/`rerank_t_rejeita` (6b) e `min_itens`/`top_n` (7) vinham do `.env`
por `ctx.config`. Agora são campos do `Params` da step, o que os coloca no formulário gerado
do Pydantic e — o que importa de verdade — sob `config_version`: versionado e imutável, para que
"por que o resultado mudou?" tenha resposta (ADR-014).

Os dois testes que sustentam a migração são `test_defaults_preservam_o_comportamento_do_env`
(a virada não muda resultado por si só) e `test_nenhuma_etapa_le_threshold_de_ctx_config`
(não sobrou uma segunda fonte para divergir em silêncio).
"""

import importlib
import inspect
from pathlib import Path

import pytest

from pesquisa_precos.steps import e6b_rerank, e7_group
from pesquisa_precos.steps import registry


# ── os valores ───────────────────────────────────────────────────────────────────────

def test_defaults_preservam_o_comportamento_do_env():
    """Os defaults têm de ser exatamente o que o `.env` carregava — a Fase 14 move a source
    do value, não o value."""
    p6b = e6b_rerank.Params()
    assert (p6b.rerank_t_aceita, p6b.rerank_t_rejeita) == (0.80, 0.30)
    p7 = e7_group.Params()
    assert (p7.min_itens, p7.top_n) == (1, 0)   # ADR-016: "regra dos 5" desativada


def test_top_n_zero_e_sem_teto_nao_zero_itens():
    """A armadilha registrada no CLAUDE.md e na ADR-016. O `ge=0` tem de aceitar o 0, e o 0
    tem de significar SEM TETO."""
    assert e7_group.Params(top_n=0).top_n == 0
    assert "SEM TETO" in e7_group.Params.model_fields["top_n"].description


def test_thresholds_validados_pelo_schema():
    """ADR-014, salvaguarda 2: a interface não pode gravar value que o código rejeitaria."""
    with pytest.raises(ValueError):
        e6b_rerank.Params(rerank_t_aceita=1.5)
    with pytest.raises(ValueError):
        e7_group.Params(min_itens=0)
    with pytest.raises(ValueError):
        e7_group.Params(top_n=-1)


# ── a source do value ────────────────────────────────────────────────────────────────

_CHAVES_MIGRADAS = ("rerank_t_aceita", "rerank_t_rejeita", "min_itens", "top_n")


def test_nenhuma_etapa_le_threshold_de_ctx_config():
    """A guarda que impede a segunda fonte de voltar. Se uma step voltar a ler o threshold do
    `.env`, o value efetivo deixa de ser o que `config_version` registra — e o run passa a
    mentir sobre o que o produziu."""
    infratores = []
    for definicao in registry.ordem():
        modulo = importlib.import_module(definicao.carregar().__name__)
        source = Path(inspect.getfile(modulo)).read_text(encoding="utf-8")
        for key in _CHAVES_MIGRADAS:
            for padrao in (f'ctx.config["{key}"]', f'cfg["{key}"]'):
                if padrao in source:
                    infratores.append(f"{definicao.key}: {padrao}")
    assert not infratores, f"threshold lido do .env em vez de Params: {infratores}"


def test_settings_so_carrega_o_bootstrap():
    """A outra metade: `settings.py` não pode voltar a expor configuração de operação. Se ele
    ganhar um leitor de `.env` de novo, o value efetivo deixa de ser o que `config_version`
    registra — e o run passa a mentir sobre o que o produziu."""
    from pesquisa_precos.config import settings

    publicos = [n for n in dir(settings) if not n.startswith("_")]
    assert "carregar_config" not in publicos
    source = Path(inspect.getfile(settings)).read_text(encoding="utf-8")
    assert "os.getenv" not in source, "settings.py voltou a ler variável de ambiente"


def test_versao_codigo_bumpada():
    """Mudar a ORIGEM do value efetivo muda o resultado potencial da step — o fingerprint
    precisa marcar as dependentes como desatualizadas."""
    assert e6b_rerank.CODE_VERSION != "2.0.0"
    assert e7_group.CODE_VERSION != "2.0.0"


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
    """`runner.launcher.preparar` resolve `Params` em três camadas (default ← config_version ←
    override do play). Este teste prova que as chaves novas entram na camada do meio — que é
    o ponto inteiro do bloco 3."""
    defaults = e7_group.Params().model_dump()
    config_valores = {"top_n": 5, "min_itens": 3, "chave_de_outra_etapa": "ignorada"}
    camada = {k: v for k, v in config_valores.items() if k in defaults}
    efetivos = e7_group.Params(**{**defaults, **camada}).model_dump()
    assert efetivos["top_n"] == 5 and efetivos["min_itens"] == 3
    assert "chave_de_outra_etapa" not in efetivos
