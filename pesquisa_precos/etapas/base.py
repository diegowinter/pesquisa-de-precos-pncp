"""
Contrato de etapa — o que toda etapa expõe e o que ela recebe do mundo externo.

Fase 1 do projeto (docs/04_FASES.md): a lógica de cada etapa sai do `main()` e vira
`executar(params, ctx)`. CLI, API e runner passam a chamar A MESMA função — não existe
caminho alternativo. O `main()` do módulo vira uma casca que só monta `Params` e o contexto.

O que a etapa NÃO faz mais por conta própria:
  - ler o `.env` (`carregar_config`)  → vem de `ctx.config`;
  - imprimir com `print`/`console`    → `ctx.log(...)`;
  - montar a própria `rich.Progress`  → `ctx.progresso(...)` (o contexto de console é quem
    decide se isso vira barra, linha de log ou UPDATE no banco);
  - gravar erro de item à mão         → `ctx.erro_item(...)`.

O `ContextoExecucao` desta fase é implementado por
`pesquisa_precos.runner.contexto_console.ContextoConsole`. Nas fases 2/3 ele ganha `db`,
`provedores`, custo real e cancelamento — por isso é um `Protocol`: a etapa nunca importa a
implementação, só o contrato.

Ver docs/03_ETAPAS.md §1 (contrato) e §1.1 (regras invioláveis da implementação).
"""

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pesquisa_precos.providers.resolver import Provedores

# Sentinela de "não mexer neste valor" para as atualizações parciais de progresso.
MANTER = ...

Acao = Literal["atualizar", "retomar", "refazer"]
Modo = Literal["assistido", "sequencial", "amostra", "simulacao"]


class TetoDeCustoExcedido(RuntimeError):
    """`ctx.gastar()` estourou o teto do run. A etapa deve parar de forma limpa.

    Só ganha efeito real na Fase 3 (tabela `llm_chamada` + teto por run); aqui existe para
    que as etapas já sejam escritas contra ele — ver ADR-004 (custo é a restrição nº 1).
    """


class Estimativa(BaseModel):
    """Resposta de `estimar()`: o que a etapa faria, sem fazer nada.

    `custo_usd` fica `None` enquanto não houver preço por chamada configurado
    (`CUSTO_USD_CHAMADA_PASS1/PASS2` no `.env`). Medição real de custo é a Fase 3 — até lá,
    inventar um número seria pior que declarar que não se sabe.
    """

    unidades: int = 0
    chamadas_llm: int = 0
    custo_usd: float | None = None
    duracao_s: float | None = None
    detalhes: dict[str, Any] = Field(default_factory=dict)


class ResultadoEtapa(BaseModel):
    """Resposta de `executar()`. `metricas` vai para a UI; `preview` alimenta o gate."""

    processados: int = 0
    erros: int = 0
    metricas: dict[str, Any] = Field(default_factory=dict)
    preview: list[dict] = Field(default_factory=list)


@runtime_checkable
class ContextoExecucao(Protocol):
    """Tudo que a etapa precisa do mundo externo. Injetado por quem executa.

    Diferença em relação ao contrato final de docs/03_ETAPAS.md §1: `db` (sessão de domínio da
    etapa, Fase 2/3) ainda não é injetado aqui — as etapas continuam abrindo sua própria sessão
    via `db.sessao()`. `provedores` (Fase 7) já é injetado: `.chat`/`.embed`/`.rerank`/`.ocr`,
    resolvidos do banco (`capacidade_provedor`) quando configurado, senão do `.env` — ver
    `providers.resolver`. `config` é, nesta fase, o dict de `config.settings.carregar_config()`
    — na Fase 6 vira a `ConfigResolvida` da `config_versao` do run, sem que a assinatura mude
    para a etapa.
    """

    acao: Acao
    modo: Modo
    config: dict
    provedores: "Provedores"

    def progresso(self, processados: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        """Avanço da unidade principal da etapa. Chamar por lote, nunca por item."""

    def log(self, nivel: str, msg: str, **contexto: Any) -> None:
        """Mensagem para o operador. `msg` pode conter markup do rich."""

    def erro_item(self, chave: str, exc: object, *, tipo: str = "", nome: str = "") -> None:
        """Falha de UMA unidade. Não derruba a etapa (docs/03_ETAPAS.md §1.1 regra 4)."""

    def cancelado(self) -> bool:
        """Checar em todo laço externo."""

    def gastar(self, usd: float) -> None:
        """Contabiliza gasto; levanta `TetoDeCustoExcedido` quando houver teto (Fase 3)."""


def subprogresso(ctx: ContextoExecucao, processados: int | None = None,
                 total: Any = MANTER, descricao: str | None = None) -> None:
    """Barra secundária, quando o contexto oferece uma (só o de console oferece).

    Existe porque a etapa 2 mostra duas coisas ao mesmo tempo — buscas (termo×fonte) e
    documentos da busca atual — e essa granularidade é o que deixa visível que a coleta está
    andando dentro de um termo demorado. Contexto que não implementa simplesmente ignora.
    """
    fn = getattr(ctx, "subprogresso", None)
    if fn is None:
        return
    fn(processados=processados, total=total, descricao=descricao)
