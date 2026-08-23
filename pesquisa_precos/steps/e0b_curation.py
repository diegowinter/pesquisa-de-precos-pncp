"""
Etapa 0b — Aplicar a curadoria: `catalogo_raw ∩ pdm_permitido` → `catalogo_item`.

Existe para separar duas decisões que a 0a fazia numa tacada só: **baixar** o catálogo
(346 mil linhas do CATMAT/CATSER, sem opinião nenhuma) e **cortar** o que interessa
(hoje ~2 mil itens). O corte é decisão do operador, não consequência automática do
download — e decisão do operador, neste projeto, é etapa com gate.

O trabalho em si é barato (dois comandos SQL): o valor da etapa é o gate. Antes de aprovar,
a tela mostra quantos itens a allow-list atual traz, e o link **Editar allow-list de PDMs**
leva a `/catalog`, onde se inclui/revoga código a código. Voltou, aprovou, o corte roda.

Entradas: `catalogo_raw` (etapa 0a) + `pdm_permitido` (a tela). Saída: `catalogo_item`.
Não é resumível nem precisa ser: recomputa o corpus inteiro em segundos, sempre.

NÃO fazer: tratar a primeira execução sem snapshot como "tudo novo" (ver
`repo.delta_catalogo`) — sem baseline, o delta é zerado por definição.
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel

from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "0b"
CODE_VERSION = "1.0.0"


class Params(BaseModel):
    """Sem parâmetros: o que esta etapa faz é definido pela allow-list, que se edita em
    `/catalog` — não por um campo de formulário."""


def run(params: Params, ctx: RunContext) -> StepResult:
    from sqlalchemy import text as text_sql

    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")

    with db.session() as s:
        total_raw = repo.contar_raw(s)
        permitidos = repo.listar_permitidos(s)
        if not permitidos:
            ctx.log("aviso", "[bold yellow]A allow-list está vazia — o corte não deixaria "
                             "nenhum item. Edite em /catalog antes de aprovar.[/]")
        ctx.progresso(0, total_raw, descricao="aplicando a allow-list")
        derivacao = repo.derivar_catalogo_item(s)
        delta = repo.delta_catalogo(s)
        s.commit()
        preview = [
            {"tipo": t, "codigo": c, "descricao": (d or "")[:80]}
            for t, c, d in s.execute(text_sql(
                "SELECT tipo::text, codigo, description FROM catalogo_item "
                "WHERE active ORDER BY tipo, codigo LIMIT 20")).all()
        ]
        ctx.progresso(total_raw, total_raw, descricao="aplicando a allow-list")

    ctx.log("info", f"[bold]Catálogo completo:[/] {total_raw:,} linhas · "
                    f"[bold green]curado: {derivacao['ativos']:,} itens[/] "
                    f"({derivacao['desativados']} desativados) · "
                    f"{len(permitidos)} códigos na allow-list")
    if delta.get("baseline"):
        ctx.log("info", "[dim]Primeiro snapshot no banco — delta zerado por definição.[/]")
    else:
        ctx.log("info", f"[bold]Delta:[/] {delta['codigos_novos']} novos, "
                        f"{delta['codigos_removidos']} removidos")

    return StepResult(
        processed=total_raw, errors=0,
        resumo=f"{derivacao['ativos']:,} itens no catálogo curado "
               f"(de {total_raw:,} do catálogo completo)",
        metrics={"itens_no_catalogo_raw": total_raw,
                 "codigos_na_allow_list": len(permitidos), **derivacao, **delta},
        preview=preview,
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Quantos itens a allow-list ATUAL deixaria passar — o número que o operador precisa
    ver antes de aprovar, e que muda toda vez que ele edita `/catalog`."""
    from sqlalchemy import text as text_sql

    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import curation as repo

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})

    with db.session() as s:
        permitidos = repo.listar_permitidos(s)
        casariam = s.execute(text_sql("""
            SELECT count(*) FROM catalogo_raw r
              JOIN pdm_permitido p
                ON p.tipo = r.tipo AND p.active
               AND p.codigo = CASE WHEN r.tipo = 'material'
                                   THEN r.codigo_pdm ELSE r.codigo END
        """)).scalar_one()
        detalhes = {
            "catalogo_completo": f"{repo.contar_raw(s):,} linhas",
            "codigos_na_allow_list": len(permitidos),
            "itens_que_passariam_no_corte": f"{casariam:,}",
        }
    return Estimate(unidades=casariam, chamadas_llm=0, cost_usd=0.0, detalhes=detalhes)
