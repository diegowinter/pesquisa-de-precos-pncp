"""
Contrato de etapa — o que toda etapa expõe e o que ela recebe do mundo externo.

Toda etapa expõe `Params`, `executar(params, ctx)` e `estimar(params, ctx)`. O que ela não faz
por conta própria:
  - imprimir com `print`/`console`    → `ctx.log(...)`;
  - montar a própria `rich.Progress`  → `ctx.progresso(...)`;
  - gravar erro de item à mão         → `ctx.item_error(...)`.

`RunContext` é um `Protocol`: a etapa nunca importa a implementação, só o contrato. Quem
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
    """`ctx.gastar()` estourou o teto do run. A etapa deve parar de forma limpa.

    Só ganha efeito real na Fase 3 (tabela `llm_call` + teto por run); aqui existe para
    que as etapas já sejam escritas contra ele — ver ADR-004 (custo é a restrição nº 1).
    """


class Estimate(BaseModel):
    """Resposta de `estimar()`: o que a etapa faria, sem fazer nada.

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
    """Resposta de `executar()`. `metrics` vai para a UI; `preview` alimenta o gate.

    `processed`/`errors` medem o TRABALHO (a unidade da barra de progresso); `resumo` conta
    o RESULTADO em uma frase, na unidade que fizer sentido para a etapa. Os dois foram um
    campo só até 2026-08-23, e a tela exibia coisas como "2.283 / 346.976" — uma fração
    entre grandezas diferentes.
    """

    processed: int = 0
    errors: int = 0
    resumo: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    preview: list[dict] = Field(default_factory=list)


@runtime_checkable
class RunContext(Protocol):
    """Tudo que a etapa precisa do mundo externo. Injetado por quem executa.

    `db` (a sessão de domínio) ainda não é injetado: as etapas abrem a própria via `db.session()`.
    `provedores` expõe `.chat`/`.embed`/`.rerank`/`.extract`/`.matching`, resolvidos do banco —
    ver `providers/resolver.py`.
    """

    action: Acao
    mode: Modo
    providers: "Providers"

    def progresso(self, processed: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        """Avanço da unidade principal da etapa. Chamar por lote, nunca por item."""

    def log(self, nivel: str, msg: str, **contexto: Any) -> None:
        """Mensagem para o operador. `msg` pode conter markup do rich."""

    def item_error(self, key: str, exc: object, *, tipo: str = "", name: str = "") -> None:
        """Falha de UMA unidade. Não derruba a etapa (docs/03_ETAPAS.md §1.1 regra 4).

        `name` é o rótulo humano do item (a descrição, o termo); `exc` é a causa. Os dois são
        gravados — passar `name` NÃO substitui a causa."""

    def cancelado(self) -> bool:
        """Checar em todo laço externo."""

    def gastar(self, usd: float) -> None:
        """Contabiliza gasto; levanta `TetoDeCustoExcedido` quando houver teto (Fase 3)."""


class Cancelada(Exception):
    """O operador clicou em Cancelar. Não é falha: a etapa grava o que já fez e retorna.

    Quem levanta é `avanco_cancelavel`; quem trata é a própria etapa, com um `except` em volta
    do laço. O `worker` decide o status final olhando `ctx.cancelado()`, não esta exceção.
    """


def avanco_cancelavel(ctx: RunContext):
    """`on_progress` que também obedece ao Cancelar — para passar a `executar_paralelo`.

    Existe porque "avançou uma unidade" é o único ponto por onde toda etapa paralela passa, e
    era exatamente onde ninguém checava o cancelamento. Cancelar a etapa 3 pela tela mudava o
    status no banco e mais nada: o pool seguia até o último dos 32 mil textos com o lock
    preso, e em 2026-08-25 deixou dois workers órfãos vivos, queimando LLM depois do Cancelar.

    O pool ainda espera as unidades em voo terminarem — é o `with ThreadPoolExecutor`. Com
    `concurrency` baixa isso é questão de segundos; o que não acontece mais é varrer a fila
    inteira.
    """
    def ao_avancar(feitos: int, total: int | None = None) -> None:
        ctx.progresso(feitos, total)
        if ctx.cancelado():
            raise Cancelada
    return ao_avancar


def subprogresso(ctx: RunContext, processed: int | None = None,
                 total: Any = MANTER, descricao: str | None = None) -> None:
    """Barra secundária, quando o contexto oferece uma (só o de console oferece).

    Existe porque a etapa 2 mostra duas coisas ao mesmo tempo — buscas (termo×fonte) e
    documentos da busca atual — e essa granularidade é o que deixa visível que a coleta está
    andando dentro de um termo demorado. Contexto que não implementa simplesmente ignora.
    """
    fn = getattr(ctx, "subprogresso", None)
    if fn is None:
        return
    fn(processed=processed, total=total, descricao=descricao)


def sem_reasoning(nome_provedor: str) -> dict:
    """Desliga o raciocínio do modelo no casamento. Mesma decisão da etapa 3.

    O casamento é transcrição estruturada, não dedução: o modelo lê uma tabela e diz quais
    candidatos estão nela. Raciocínio aqui gasta tokens de SAÍDA (os caros), aumenta a latência
    — e a etapa 5 já é o gargalo do ciclo — e traz variância que não melhora a resposta.

    Os dois formatos existem porque não são intercambiáveis: o LM Studio IGNORA o objeto
    `reasoning` do OpenRouter, e só entende `reasoning_effort` no corpo cru.
    """
    if nome_provedor == "local":
        return {"extra_body": {"reasoning_effort": "none"}}
    return {"reasoning": {"enabled": False}}
