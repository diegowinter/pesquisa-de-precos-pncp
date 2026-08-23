"""
Guarda da Fase 1: o contrato de etapa e o registry estão de pé.

O modo de falha específico desta fase é silencioso do mesmo jeito que o da Fase 0: uma etapa
que não expõe `executar`, um `Params` cujo campo não vira campo de formulário, ou uma dependência escrita
errada no registry não quebram nada até alguém rodar a etapa — provavelmente depois de já ter
pago pela anterior. Estes testes fazem isso aparecer em segundos.

Não testam regra de negócio (isso é a Fase 9) — testam fiação.
"""

import inspect

import pytest
from pydantic import BaseModel

from pesquisa_precos.steps import registry
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult
from pesquisa_precos.runner.null_context import NullContext

CHAVES = [e.chave for e in registry.todas()]


@pytest.mark.parametrize("chave", CHAVES)
def test_etapa_cumpre_o_contrato(chave):
    """KEY, CODE_VERSION, Params, executar(params, ctx) e estimar(params, ctx)."""
    definicao = registry.obter(chave)
    mod = definicao.carregar()
    assert mod.KEY == chave, f"{definicao.modulo}.KEY diverge do registry"
    assert isinstance(mod.CODE_VERSION, str) and mod.CODE_VERSION
    assert issubclass(mod.Params, BaseModel)
    for name in ("run", "estimate"):
        fn = getattr(mod, name)
        assert list(inspect.signature(fn).parameters) == ["params", "ctx"], (
            f"{definicao.modulo}.{name} deve receber (params, ctx)")


@pytest.mark.parametrize("chave", CHAVES)
def test_params_tem_defaults_para_tudo(chave):
    """`estimar` e o gate instanciam `Params()` sem argumentos — nenhum campo pode ser exigido."""
    registry.obter(chave).params_model()


@pytest.mark.parametrize("chave", CHAVES)
def test_todo_campo_de_params_aparece_no_formulario(chave):
    """Fase 13: o formulário da web é a ÚNICA superfície de configuração (não há mais flag de
    CLI). Um campo de `Params` que não chega a `schema_parametros` fica inconfigurável — e o
    sintoma seria a etapa rodar com o default sem ninguém perceber."""
    from pesquisa_precos.services.config import schema_parametros

    modelo = registry.obter(chave).params_model
    campos = schema_parametros()[chave]["campos"]
    assert list(campos) == list(modelo.model_fields)


def test_ordem_topologica_resolve_e_e_completa():
    ordem = registry.ordem()
    assert len(ordem) == len(registry.todas())
    vistas: set[str] = set()
    for etapa in ordem:
        assert set(etapa.depende_de) <= vistas, f"{etapa.chave} vem antes de suas dependências"
        vistas.add(etapa.chave)


def test_dependencias_apontam_para_etapas_existentes():
    chaves = set(CHAVES)
    for etapa in registry.todas():
        assert set(etapa.depende_de) <= chaves, etapa.chave


def test_etapa_paga_nunca_e_silenciosa():
    """Etapa `pago` sem gate seria dinheiro saindo sem ninguém confirmar (ADR-004).

    A 5 é a exceção conhecida e aceita: ela roda depois do gate da 4, que é justamente onde o
    volume da extração é aprovado.
    """
    sem_gate = [e.chave for e in registry.todas() if e.custo == "pago" and not e.precisa_gate]
    assert sem_gate == ["5"], sem_gate


def test_6c_usa_o_modelo_barato_por_padrao():
    """Restrição nº 1 do projeto: o comportamento seguro não depende de digitar uma flag."""
    params = registry.obter("6c").params_model()
    assert params.forte is False


def test_top_n_zero_e_sem_teto_e_nao_e_rejeitado_pela_validacao():
    """ADR-016 — `top_n=0` significa SEM TETO; o schema não pode tratá-lo como inválido."""
    assert registry.obter("7").params_model(top_n=0).top_n == 0


def test_contexto_console_satisfaz_o_protocolo():
    assert isinstance(NullContext("teste"), RunContext)


def test_modelos_de_resultado_tem_defaults():
    assert StepResult().processados == 0
    assert Estimate().custo_usd is None
