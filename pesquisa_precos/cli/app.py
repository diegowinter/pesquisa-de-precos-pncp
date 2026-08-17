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

import json

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

# Fase 3 (docs/04_FASES.md): "execução observável" — a mesma etapa, mas rodada via banco
# (run/run_etapa/lock/heartbeat/custo) em vez do ContextoConsole direto. `etapa <chave>`
# continua sendo o caminho do dia a dia (CLAUDE.md §"quem roda a pipeline é o usuário"); `run
# *` é o que a Fase 4 (API) vai chamar por baixo, e que já dá pra testar hoje pela CLI.
run_app = typer.Typer(add_completion=False, help="Executa uma etapa via banco (run/run_etapa).")
app.add_typer(run_app, name="run")


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


@run_app.command("criar")
def cmd_run_criar(
    rotulo: str = typer.Option(..., "--rotulo", help="Nome legível do run"),
    modo: str = typer.Option("assistido", "--modo",
                             help="assistido|sequencial|amostra|simulacao"),
    teto_custo_usd: float | None = typer.Option(
        None, "--teto-custo-usd", help="Aborta a etapa de forma limpa ao ultrapassar (ADR-004)"),
    config_rotulo: str = typer.Option("default", "--config", help="Rótulo da config_versao"),
):
    """Cria um `run` novo, apontando para uma `config_versao` (cria 'default' se não existir)."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo

    with db.sessao() as sessao:
        cv = repo.config_versao_por_rotulo(sessao, config_rotulo)
        if cv is None:
            cv = repo.criar_config_versao(sessao, config_rotulo, notas="criada pela CLI")
        run_id = repo.criar_run(sessao, rotulo, cv, modo=modo)
        if teto_custo_usd is not None:
            from sqlalchemy import text
            sessao.execute(text("UPDATE run SET teto_custo_usd = :t WHERE id = :id"),
                           {"t": teto_custo_usd, "id": run_id})
    console.print(f"[bold green]run {run_id}[/] criado (rótulo={rotulo!r}, modo={modo}, "
                  f"config={config_rotulo!r})")


@run_app.command("etapa")
def cmd_run_etapa(
    run_id: int = typer.Argument(..., help="Id do run (ver `run criar`)"),
    chave: str = typer.Argument(..., help="Chave da etapa (0a, 1, 2, 3, ...)"),
    acao: str = typer.Option("atualizar", "--acao", help="atualizar|retomar|refazer"),
    definir: list[str] = typer.Option(  # noqa: B008 — padrão do Typer, não mutável na prática
        [], "--set", help="Override de param, 'chave=valor_json' (repetível)"),
    background: bool = typer.Option(
        False, "--background", help="Não espera o subprocesso terminar"),
):
    """Sobe a etapa como SUBPROCESSO (ADR-002), com lock, heartbeat, lease e custo no banco —
    é o caminho que a API (Fase 4) vai chamar. Mate o processo (Ctrl+C ou `kill`) e rode de
    novo com `--acao retomar` para conferir que nada se perde nem duplica."""
    from pesquisa_precos.runner import executor, lock

    override: dict = {}
    for item in definir:
        if "=" not in item:
            raise typer.BadParameter(f"formato esperado 'chave=valor_json': {item!r}")
        k, v = item.split("=", 1)
        try:
            override[k] = json.loads(v)
        except json.JSONDecodeError:
            override[k] = v

    try:
        run_etapa_id, processo = executor.tocar(run_id, chave, acao=acao,
                                                params_override=override)
    except lock.LockOcupado as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(1) from None

    console.print(f"[dim]run_etapa={run_etapa_id} pid={processo.pid} — "
                  f"acompanhe com `run status {run_id} {chave}`[/]")
    if background:
        return
    try:
        codigo = processo.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl+C — o subprocesso continua rodando em segundo plano "
                      "(ele não recebe o sinal do terminal pai neste modo). Mate-o pelo pid "
                      "acima se quiser testar a retomada.[/]")
        raise typer.Exit(130) from None
    resultado = executor.status(run_etapa_id)
    cor = {"concluida": "green", "cancelada": "yellow"}.get(resultado["status"], "red")
    console.print(f"[bold {cor}]run_etapa {run_etapa_id} → {resultado['status']}[/] "
                  f"(processados={resultado['processados']}, erros={resultado['erros']}, "
                  f"custo=US$ {resultado['custo_usd']})")
    raise typer.Exit(codigo)


@run_app.command("cancelar")
def cmd_run_cancelar(run_id: int, chave: str):
    """`UPDATE` que marca `cancelada` — o subprocesso em execução percebe no próprio
    `ctx.cancelado()` (ADR-005: gate/cancelamento não segura lock, é só um UPDATE)."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo

    with db.sessao() as sessao:
        run_etapa_id = repo.obter_ou_criar_run_etapa(sessao, run_id, chave)
        ok = repo.solicitar_cancelamento(sessao, run_etapa_id)
    console.print("[yellow]cancelamento solicitado[/]" if ok
                  else "[dim]nada a cancelar — a etapa não está executando[/]")


@run_app.command("status")
def cmd_run_status(run_id: int, chave: str | None = typer.Argument(None)):
    """Mostra `run_etapa` (progresso, custo, status) — o que `SELECT * FROM run_etapa` daria."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo
    from sqlalchemy import text

    with db.sessao() as sessao:
        if chave:
            linhas = [repo.run_etapa_por_id(
                sessao, repo.obter_ou_criar_run_etapa(sessao, run_id, chave))]
        else:
            linhas = [dict(r) for r in sessao.execute(
                text("SELECT id, run_id, etapa, status, processados, erros, custo_usd, "
                     "heartbeat_em, pid FROM run_etapa WHERE run_id = :r ORDER BY id"),
                {"r": run_id}).mappings().all()]
    tabela = Table(title=f"run {run_id}")
    for coluna in ("etapa", "status", "processados", "erros", "custo_usd", "heartbeat_em", "pid"):
        tabela.add_column(coluna)
    for linha in linhas:
        tabela.add_row(*(str(linha.get(c, "")) for c in
                         ("etapa", "status", "processados", "erros", "custo_usd",
                          "heartbeat_em", "pid")))
    console.print(tabela)


@run_app.command("custo")
def cmd_run_custo(run_id: int):
    """Custo acumulado do run — `run.custo_usd`, contra o qual `teto_custo_usd` é comparado."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo

    with db.sessao() as sessao:
        run = repo.run_por_id(sessao, run_id)
        custo = repo.custo_run(sessao, run_id)
    teto = "sem teto" if run["teto_custo_usd"] is None else f"US$ {run['teto_custo_usd']}"
    console.print(f"run {run_id}: gasto US$ {custo} (teto: {teto})")


@run_app.command("log")
def cmd_run_log(run_id: int, etapa: str | None = typer.Option(None, "--etapa"),
                n: int = typer.Option(50, "--n")):
    """Últimas linhas de `run_log` — log estruturado (JSON em `contexto`, `run_id`/`etapa`
    em toda linha por construção, não por convenção)."""
    from pesquisa_precos.db import sessao as db
    from pesquisa_precos.db.repos import execucao as repo

    with db.sessao() as sessao:
        linhas = repo.logs_do_run(sessao, run_id, etapa=etapa, limite=n)
    for linha in reversed(linhas):
        sufixo = f" {linha['contexto']}" if linha["contexto"] else ""
        console.print(f"[dim]{linha['criado_em']}[/] [{linha['etapa']}] {linha['mensagem']}"
                      f"{sufixo}")


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
