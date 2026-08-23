"""
Contrato de step — o que toda step expõe e o que ela recebe do mundo externo.

Toda step expõe `Params`, `executar(params, ctx)` e `estimar(params, ctx)`. O que ela não faz
por conta própria:
  - imprimir com `print`/`console`    → `ctx.log(...)`;
  - montar a própria `rich.Progress`  → `ctx.progresso(...)`;
  - gravar erro de item à mão         → `ctx.erro_item(...)`.

`RunContext` é um `Protocol`: a step nunca importa a implementação, só o contrato. Quem
implementa é `runner/contexto_banco.py` (durante um run) e `runner/contexto_nulo.py` (para
`estimar()`, fora de um run).

Ver docs/03_ETAPAS.md §1 (contrato) e §1.1 (regras invioláveis da implementação).
"""

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pesquisa_precos.providers.resolver import Providers

# Sentinela de "não mexer neste valor" para as atualizações parciais de progresso.
MANTER = ...

Acao = Literal["update", "resume", "redo"]
Modo = Literal["assisted", "sequential", "sample", "simulation"]


class TetoDeCustoExcedido(RuntimeError):
    """`ctx.gastar()` estourou o teto do run. A step deve parar de forma limpa.

    Só ganha efeito real na Fase 3 (tabela `llm_call` + teto por run); aqui existe para
    que as etapas já sejam escritas contra ele — ver ADR-004 (custo é a restrição nº 1).
    """


class Estimate(BaseModel):
    """Resposta de `estimar()`: o que a step faria, sem fazer nada.

    `cost_usd` fica `None` enquanto não houver preço por chamada configurado
    (`CUSTO_USD_CHAMADA_PASS1/PASS2` no `.env`). Medição real de custo é a Fase 3 — até lá,
    inventar um número seria pior que declarar que não se sabe.
    """

    unidades: int = 0
    chamadas_llm: int = 0
    cost_usd: float | None = None
    duracao_s: float | None = None
    detalhes: dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    """Resposta de `executar()`. `metrics` vai para a UI; `preview` alimenta o gate."""

    processed: int = 0
    erros: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    preview: list[dict] = Field(default_factory=list)


@runtime_checkable
class RunContext(Protocol):
    """Tudo que a step precisa do mundo externo. Injetado por quem executa.

    `db` (a sessão de domínio) ainda não é injetado: as etapas abrem a própria via `db.session()`.
    `provedores` expõe `.chat`/`.embed`/`.rerank`/`.pdf`/`.matching`, resolvidos do banco —
    ver `providers/resolver.py`.
    """

    action: Acao
    mode: Modo
    providers: "Providers"

    def progresso(self, processed: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        """Avanço da unidade principal da step. Chamar por lote, nunca por item."""

    def log(self, nivel: str, msg: str, **contexto: Any) -> None:
        """Mensagem para o operador. `msg` pode conter markup do rich."""

    def item_error(self, key: str, exc: object, *, tipo: str = "", name: str = "") -> None:
        """Falha de UMA unidade. Não derruba a step (docs/03_ETAPAS.md §1.1 regra 4)."""

    def cancelado(self) -> bool:
        """Checar em todo laço externo."""

    def gastar(self, usd: float) -> None:
        """Contabiliza gasto; levanta `TetoDeCustoExcedido` quando houver teto (Fase 3)."""


def subprogresso(ctx: RunContext, processed: int | None = None,
                 total: Any = MANTER, descricao: str | None = None) -> None:
    """Barra secundária, quando o contexto oferece uma (só o de console oferece).

    Existe porque a step 2 mostra duas coisas ao mesmo tempo — buscas (termo×fonte) e
    documentos da busca atual — e essa granularidade é o que deixa visível que a coleta está
    andando dentro de um termo demorado. Contexto que não implementa simplesmente ignora.
    """
    fn = getattr(ctx, "subprogresso", None)
    if fn is None:
        return
    fn(processed=processed, total=total, descricao=descricao)
