"""
Guarda da Fase 1: o contrato de step e o registry estão de pé.

O mode de falha específico desta fase é silencioso do mesmo jeito que o da Fase 0: uma step
que não expõe `executar`, um `Params` cujo campo não vira campo de formulário, ou uma dependência escrita
errada no registry não quebram nada até alguém rodar a step — provavelmente depois de já ter
pago pela anterior. Estes testes fazem isso aparecer em segundos.

Não testam regra de negócio (isso é a Fase 9) — testam fiação.
"""

import inspect

import pytest
from pydantic import BaseModel

from pesquisa_precos.steps import registry
from pesquisa_precos.steps.base import RunContext, Estimate, StepResult
from pesquisa_precos.runner.null_context import NullContext

CHAVES = [e.key for e in registry.todas()]


@pytest.mark.parametrize("key", CHAVES)
def test_etapa_cumpre_o_contrato(key):
    """KEY, CODE_VERSION, Params, executar(params, ctx) e estimar(params, ctx)."""
    definicao = registry.obter(key)
    mod = definicao.carregar()
    assert mod.KEY == key, f"{definicao.modulo}.KEY diverge do registry"
    assert isinstance(mod.CODE_VERSION, str) and mod.CODE_VERSION
    assert issubclass(mod.Params, BaseModel)
    for name in ("run", "estimate"):
        fn = getattr(mod, name)
        assert list(inspect.signature(fn).parameters) == ["params", "ctx"], (
            f"{definicao.modulo}.{name} deve receber (params, ctx)")


@pytest.mark.parametrize("key", CHAVES)
def test_params_tem_defaults_para_tudo(key):
    """`estimar` e o gate instanciam `Params()` sem argumentos — nenhum campo pode ser exigido."""
    registry.obter(key).params_model()


@pytest.mark.parametrize("key", CHAVES)
def test_todo_campo_de_params_aparece_no_formulario(key):
    """Fase 13: o formulário da web é a ÚNICA superfície de configuração (não há mais flag de
    CLI). Um campo de `Params` que não chega a `schema_parametros` fica inconfigurável — e o
    sintoma seria a step rodar com o default sem ninguém perceber."""
    from pesquisa_precos.services.config import schema_parametros

    model = registry.obter(key).params_model
    campos = schema_parametros()[key]["campos"]
    assert list(campos) == list(model.model_fields)


def test_ordem_topologica_resolve_e_e_completa():
    ordem = registry.ordem()
    assert len(ordem) == len(registry.todas())
    vistas: set[str] = set()
    for step in ordem:
        assert set(step.depends_on) <= vistas, f"{step.key} vem antes de suas dependências"
        vistas.add(step.key)


def test_dependencias_apontam_para_etapas_existentes():
    chaves = set(CHAVES)
    for step in registry.todas():
        assert set(step.depends_on) <= chaves, step.key


def test_etapa_paga_nunca_e_silenciosa():
    """Etapa `pago` sem gate seria dinheiro saindo sem ninguém confirm (ADR-004).

    A 5 é a exceção conhecida e aceita: ela roda depois do gate da 4, que é justamente onde o
    volume da extração é aprovado.
    """
    sem_gate = [e.key for e in registry.todas() if e.custo == "pago" and not e.precisa_gate]
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
    assert StepResult().processed == 0
    assert Estimate().cost_usd is None


# ── Atributos que a etapa lê de `params` e de `ctx` ──────────────────────────────────
#
# O rename pt→en de 2026-08-22 deixou seis sobras (`params.provedor`, `resultado.metricas`,
# `resolucao.origem`, ...) e TODAS só apareceram em runtime, no meio de um teste assistido:
# são linhas de log e caminhos de erro que nenhum teste percorre. Uma leitura estática do
# AST pega a próxima sem precisar executar a etapa.

def _atributos_lidos(modulo, nome_variavel: str) -> list[tuple[int, str]]:
    import ast
    import pathlib

    fonte = pathlib.Path(modulo.__file__).read_text(encoding="utf-8")
    return [(no.lineno, no.attr) for no in ast.walk(ast.parse(fonte))
            if isinstance(no, ast.Attribute) and isinstance(no.value, ast.Name)
            and no.value.id == nome_variavel]


@pytest.mark.parametrize("key", CHAVES)
def test_etapa_so_le_campos_que_o_proprio_params_declara(key):
    definicao = registry.obter(key)
    modulo = definicao.carregar()
    permitidos = set(definicao.params_model.model_fields) | set(dir(definicao.params_model))
    erradas = [f"linha {linha}: params.{attr}"
               for linha, attr in _atributos_lidos(modulo, "params")
               if attr not in permitidos]
    assert not erradas, f"step {key} lê campo inexistente em Params: {erradas}"


@pytest.mark.parametrize("key", CHAVES)
def test_etapa_so_usa_o_que_o_runcontext_oferece(key):
    """`ctx.config` foi removido na Fase 14 e `ctx.subprogresso` é opcional — o resto tem de
    estar no Protocol, senão a etapa quebra na primeira linha de log que o chame."""
    modulo = registry.obter(key).carregar()
    # `dir()` não enxerga anotação sem valor (`providers: "Providers"`).
    permitidos = (set(dir(RunContext)) | set(RunContext.__annotations__)
                  | {"subprogresso"})
    erradas = [f"linha {linha}: ctx.{attr}"
               for linha, attr in _atributos_lidos(modulo, "ctx")
               if attr not in permitidos]
    assert not erradas, f"step {key} usa algo fora do RunContext: {erradas}"
