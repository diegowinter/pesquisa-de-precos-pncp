"""
`RunContext` que não mostra nada — o contexto de `estimar()`.

`estimar(params, ctx)` tem a mesma assinatura de `executar(params, ctx)` de propósito (é o
contrato da Fase 1), mas roda FORA de um run: ninguém criou `run_step`, não há barra para
alimentar nem `run_log` para escrever. Este contexto existe para preencher esse buraco — ele
aceita todas as chamadas do contrato e descarta o que é apresentação.

Até a Fase 13 o papel era do `ContextoConsole`, instanciado com `mostrar_barra=False` só para
isso. Com a CLI fora, `rich` não tem mais para onde imprimir: quem consome a estimativa é o
formulário da web, que lê o `Estimate` de volta.

`gastar()` continua contando e continua respeitando o teto (ADR-004): `estimar` não deveria
gastar nada, e se uma etapa gastar, o teto ainda a interrompe.
"""

from pesquisa_precos.steps.base import MANTER, TetoDeCustoExcedido
from pesquisa_precos.providers.resolver import Providers


class NullContext:
    """Contexto silencioso. Use como contexto manager para simetria com os outros."""

    def __init__(self, step: str, *,
                 action: str = "update", mode: str = "assisted",
                 cost_cap_usd: float | None = None, providers_session=None):
        self.step = step
        self.action = action
        self.mode = mode
        self.teto_custo_usd = cost_cap_usd
        self.providers = Providers(providers_session)

        self.gasto_usd = 0.0
        self.n_erros = 0
        self.ultimo_progresso: tuple[int, int | None] = (0, None)
        self._cancelado = False

    # ── ciclo de vida ────────────────────────────────────────────────────────────
    def __enter__(self) -> "NullContext":
        return self

    def __exit__(self, *exc) -> None:
        self.fechar()

    def fechar(self) -> None:
        pass

    # ── contrato RunContext ────────────────────────────────────────────────
    def progresso(self, processed: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        self.ultimo_progresso = (processed, total if total is not None
                                 else self.ultimo_progresso[1])

    def subprogresso(self, processed: int | None = None, total=MANTER,
                     descricao: str | None = None) -> None:
        pass

    def avancar_subprogresso(self, n: int = 1) -> None:
        pass

    def log(self, nivel: str, msg: str, **contexto) -> None:
        pass

    def item_error(self, key: str, exc: object, *, tipo: str = "", name: str = "") -> None:
        # Conta, mas não persiste: erro por item vive em `item_error`, e quem grava lá é o
        # `DbContext`, dentro de um run. Uma estimativa não tem run a que pendurar erro.
        self.n_erros += 1

    def cancelado(self) -> bool:
        return self._cancelado

    def cancelar(self) -> None:
        self._cancelado = True

    def gastar(self, usd: float) -> None:
        self.gasto_usd += usd
        if self.teto_custo_usd is not None and self.gasto_usd > self.teto_custo_usd:
            raise TetoDeCustoExcedido(
                f"teto de US$ {self.teto_custo_usd:.2f} excedido (gasto US$ {self.gasto_usd:.2f})")
