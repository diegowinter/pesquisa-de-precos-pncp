"""
CLI do pipeline — casca fina sobre `etapas/`.

Três comandos, os três do critério de aceite da Fase 1 (docs/04_FASES.md):

    python -m pesquisa_precos.cli etapa 3 --concurrency 8   # executa
    python -m pesquisa_precos.cli estimar 3                 # escopo e custo, sem gastar
    python -m pesquisa_precos.cli grafo                     # ordem/dependências do registry

Nada de lógica de etapa aqui: este módulo resolve a etapa no registry, monta `Params` a
partir das flags (geradas do próprio schema — ver `flags.py`), cria o `ContextoConsole` e
chama `executar()`/`estimar()`. É a mesma chamada que a API e o runner farão nas fases
seguintes; se algum comportamento morar aqui, ele não existe pela API.

`main()` de cada módulo de etapa entra por `rodar_etapa_isolada()`, o que mantém
`python -m pesquisa_precos.etapas.e3_classificar --provedor local` funcionando exatamente
como antes (é o que `rodar.py` invoca, e é o que está no dedo do usuário).
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import typer
from rich.console import Console
from rich.table import Table

from pesquisa_precos.cli.flags import comando_para
from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.etapas import registry
from pesquisa_precos.runner.contexto_console import ContextoConsole

console = Console()

app = typer.Typer(add_completion=False, help="Pipeline de pesquisa de preços PLASEG (PNCP).")
etapa_app = typer.Typer(add_completion=False, help="Executa uma etapa.")
app.add_typer(etapa_app, name="etapa")


def _contexto(definicao: registry.DefinicaoEtapa, *, acao: str = "atualizar") -> ContextoConsole:
    return ContextoConsole(
        definicao.chave, console=console, config=carregar_config(),
        caminho_erros=definicao.caminho_erros, acao=acao,
    )


def executar_etapa(definicao: registry.DefinicaoEtapa, params) -> int:
    """Roda uma etapa e imprime o resumo. Devolve o código de saída do processo."""
    modulo = definicao.carregar()
    with _contexto(definicao) as ctx:
        try:
            resultado = modulo.executar(params, ctx)
        except KeyboardInterrupt:
            ctx.fechar()
            # SIGINT (Ctrl+C, terminal fechado, suspensão da máquina). O progresso já está em
            # disco (fsync por linha): saímos com 130 para o laço de auto-restart distinguir
            # "interrompido" (relança) de "concluído" (0).
            console.print(f"\n[yellow][{definicao.chave}] Interrompido (SIGINT) — progresso "
                          f"salvo, é resumível. Rode de novo para continuar de onde parou.[/]")
            return 130
    if resultado is None:
        return 0
    cor = "yellow" if resultado.erros else "green"
    metricas = " · ".join(f"{k}={v}" for k, v in resultado.metricas.items())
    console.print(f"[bold {cor}][{definicao.chave}] {resultado.processados} processados, "
                  f"{resultado.erros} erros.[/]" + (f" [dim]{metricas}[/]" if metricas else ""))
    return 0


def _registrar_comandos_de_etapa() -> None:
    for definicao in registry.todas():
        def corpo(params, _def=definicao):
            raise typer.Exit(executar_etapa(_def, params))

        etapa_app.command(
            name=definicao.chave, help=definicao.titulo,
            # O corpo é criado por etapa; o modelo só é importado quando a etapa é chamada.
        )(comando_para(definicao.params_model, corpo))


@app.command("estimar")
def cmd_estimar(chave: str = typer.Argument(..., help="Chave da etapa (0a, 1, 2, 3, ...)")):
    """Escopo e custo previstos da etapa — não chama provedor pago e não grava nada."""
    definicao = registry.obter(chave)
    modulo = definicao.carregar()
    params = definicao.params_model()
    with _contexto(definicao) as ctx:
        est = modulo.estimar(params, ctx)
    custo = "não estimado (defina CUSTO_USD_CHAMADA_PASS1/PASS2 no .env)" \
        if est.custo_usd is None else f"US$ {est.custo_usd:,.2f}"
    duracao = "—" if est.duracao_s is None else f"~{est.duracao_s / 60:,.0f} min"
    console.print(f"[bold]Etapa {definicao.chave} — {definicao.titulo}[/] "
                  f"[dim](custo: {definicao.custo})[/]")
    console.print(f"  unidades a processar : [bold]{est.unidades:,}[/]")
    console.print(f"  chamadas de LLM      : [bold]{est.chamadas_llm:,}[/]")
    console.print(f"  custo estimado       : {custo}")
    console.print(f"  duração estimada     : {duracao}")
    for k, v in est.detalhes.items():
        console.print(f"  [dim]{k}: {v}[/]")


@app.command("grafo")
def cmd_grafo():
    """Desenha a ordem das etapas e suas dependências, a partir do registry."""
    tabela = Table(title="Etapas do pipeline (ordem topológica do registry)")
    for coluna in ("Etapa", "Título", "Depende de", "Custo", "Gate", "Recomputa corpus"):
        tabela.add_column(coluna)
    for definicao in registry.ordem():
        cor = {"pago": "red", "gpu": "magenta", "cpu": "cyan", "gratis": "green"}[definicao.custo]
        tabela.add_row(
            definicao.chave, definicao.titulo,
            " + ".join(definicao.depende_de) or "—",
            f"[{cor}]{definicao.custo}[/]",
            "⏸ sim" if definicao.precisa_gate else "—",
            "sim" if definicao.recomputa_corpus else "—",
        )
    console.print(tabela)
    console.print("[dim]Refazer uma etapa deixa desatualizadas as que dependem dela:[/]")
    for definicao in registry.ordem():
        deps = registry.dependentes(definicao.chave)
        if deps:
            console.print(f"  [dim]{definicao.chave:>2} → {', '.join(deps)}[/]")


def rodar_etapa_isolada(chave: str) -> None:
    """Ponto de entrada de `python -m pesquisa_precos.etapas.e*` (o `main()` de cada etapa)."""
    definicao = registry.obter(chave)

    def corpo(params):
        raise typer.Exit(executar_etapa(definicao, params))

    isolado = typer.Typer(add_completion=False)
    isolado.command(help=definicao.titulo)(comando_para(definicao.params_model, corpo))
    isolado(prog_name=f"etapa {chave}")


def main() -> None:
    _registrar_comandos_de_etapa()
    app()
