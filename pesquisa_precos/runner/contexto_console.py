"""
`ContextoExecucao` de console — a implementação da Fase 1.

É o que faz `executar(params, ctx)` continuar parecendo, no terminal, exatamente com o que o
usuário já conhece: uma `rich.Progress` viva, mensagens coloridas acima dela e erros de item
em `data/erros/`. A diferença é que agora quem sabe disso é o contexto, não a etapa — na
Fase 3 o mesmo `executar()` roda com um contexto que escreve progresso em `run_etapa` e log
em `run_log`, sem tocar no corpo de nenhuma etapa.

Detalhe que não é cosmético: a `Console` do contexto é a MESMA usada pela barra. Rich só
consegue imprimir "acima" de um display vivo se o print sair pelo console do display; duas
`Console()` distintas embaralham a saída. Por isso a etapa loga por `ctx.log()` e não por um
`console` próprio de módulo.
"""

from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from pesquisa_precos.core import erros_log
from pesquisa_precos.etapas.base import MANTER, TetoDeCustoExcedido

_ESTILO = {"aviso": "yellow", "erro": "bold red", "debug": "dim"}


class ContextoConsole:
    """Contexto de execução para a CLI. Use como context manager (fecha a barra no fim)."""

    def __init__(self, etapa: str, *, console: Console | None = None,
                 config: dict | None = None, caminho_erros: Path | None = None,
                 acao: str = "atualizar", modo: str = "assistido",
                 teto_custo_usd: float | None = None, mostrar_barra: bool = True):
        self.etapa = etapa
        self.acao = acao
        self.modo = modo
        self.console = console or Console()
        self.config = config if config is not None else {}
        self.caminho_erros = caminho_erros
        self.teto_custo_usd = teto_custo_usd
        self.mostrar_barra = mostrar_barra

        self.gasto_usd = 0.0
        self.n_erros = 0
        self.ultimo_progresso: tuple[int, int | None] = (0, None)

        self._cancelado = False
        self._barra: Progress | None = None
        self._tarefa = None
        self._subtarefa = None

    # ── ciclo de vida ────────────────────────────────────────────────────────────
    def __enter__(self) -> "ContextoConsole":
        return self

    def __exit__(self, *exc) -> None:
        self.fechar()

    def fechar(self) -> None:
        if self._barra is not None:
            self._barra.stop()
            self._barra = None
            self._tarefa = self._subtarefa = None

    def _garantir_barra(self) -> Progress:
        if self._barra is None:
            self._barra = Progress(
                SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=30),
                MofNCompleteColumn(), TimeElapsedColumn(),
                console=self.console, disable=not self.mostrar_barra,
            )
            self._barra.start()
        return self._barra

    # ── contrato ContextoExecucao ────────────────────────────────────────────────
    def progresso(self, processados: int, total: int | None = None,
                  descricao: str | None = None) -> None:
        self.ultimo_progresso = (processados, total if total is not None
                                 else self.ultimo_progresso[1])
        barra = self._garantir_barra()
        if self._tarefa is None:
            self._tarefa = barra.add_task(descricao or f"etapa {self.etapa}", total=total)
        campos: dict = {"completed": processados}
        if total is not None:
            campos["total"] = total
        if descricao is not None:
            campos["description"] = descricao
        barra.update(self._tarefa, **campos)

    def subprogresso(self, processados: int | None = None, total=MANTER,
                     descricao: str | None = None) -> None:
        """Barra secundária (ver `etapas.base.subprogresso`). `total=MANTER` não mexe no total."""
        barra = self._garantir_barra()
        if self._subtarefa is None:
            self._subtarefa = barra.add_task(descricao or "", total=None)
        campos: dict = {}
        if processados is not None:
            campos["completed"] = processados
        if total is not MANTER:
            campos["total"] = total
        if descricao is not None:
            campos["description"] = descricao
        if campos:
            barra.update(self._subtarefa, **campos)

    def avancar_subprogresso(self, n: int = 1) -> None:
        """Conveniência do console: +n na barra secundária (a etapa 2 conta documento a documento)."""
        barra = self._garantir_barra()
        if self._subtarefa is None:
            self._subtarefa = barra.add_task("", total=None)
        barra.advance(self._subtarefa, n)

    def log(self, nivel: str, msg: str, **contexto) -> None:
        # A mensagem já vem com o markup que a etapa quer; `nivel` só acrescenta cor quando a
        # etapa não pintou nada. Contexto extra vira sufixo discreto (vira JSON na Fase 3).
        estilo = _ESTILO.get(nivel)
        sufixo = ""
        if contexto:
            sufixo = " [dim](" + ", ".join(f"{k}={v}" for k, v in contexto.items()) + ")[/]"
        alvo = self._barra.console if self._barra is not None else self.console
        alvo.print(f"[{estilo}]{msg}[/]{sufixo}" if estilo else f"{msg}{sufixo}")

    def erro_item(self, chave: str, exc: object, *, tipo: str = "", nome: str = "") -> None:
        self.n_erros += 1
        if self.caminho_erros is None:
            return
        self.caminho_erros.parent.mkdir(parents=True, exist_ok=True)
        erros_log.logar_erro(str(self.caminho_erros), self.etapa, tipo, chave, nome, exc)

    def cancelado(self) -> bool:
        return self._cancelado

    def cancelar(self) -> None:
        self._cancelado = True

    def gastar(self, usd: float) -> None:
        self.gasto_usd += usd
        if self.teto_custo_usd is not None and self.gasto_usd > self.teto_custo_usd:
            raise TetoDeCustoExcedido(
                f"teto de US$ {self.teto_custo_usd:.2f} excedido (gasto US$ {self.gasto_usd:.2f})")
