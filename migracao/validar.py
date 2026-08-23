"""
Validação por agregado — docs/05_MIGRACAO.md §4.

"Migrou, deve estar ok" é explicitamente proibido. Este módulo responde duas perguntas
diferentes, e a distinção entre elas é o ponto:

  **Contagem** — quantas linhas chegaram, contra os números medidos no acervo em 2026-08-16.
  Divergência aqui é ACEITÁVEL desde que explicada (duplicata no CSV, dedup por chave natural,
  linha órfã descartada). Cada script de migração já reporta a sua explicação.

  **Integridade referencial** — órfãos. Aqui **toda contagem tem de ser zero**. Um item sem
  documento, um par sem item ou um grupo sem par significa que a FK foi contornada em algum
  lugar, e o sintoma seguinte seria um export com linha faltando ou preço errado.

Uso: python -m migracao.validar
"""

import sys

from rich.table import Table
from sqlalchemy import text as sql

from pesquisa_precos.db import session as db
from migracao._comum import console

# (rótulo, esperado, SQL). Os esperados vieram de contagem no acervo real (02_SCHEMA.md §1),
# não de estimativa — manter esse padrão ao atualizá-los.
CONTAGENS = (
    ("catalogo_item",      2_212,     "SELECT count(*) FROM catalogo_item"),
    ("termo",                499,     "SELECT count(*) FROM termo"),
    ("documento",         68_163,     "SELECT count(*) FROM documento"),
    ("item",           1_613_517,     "SELECT count(*) FROM item"),
    ("item_sobrevivente", 302_514,    "SELECT count(*) FROM item WHERE sobrevivente"),
    ("texto_classificacao", 320_000,  "SELECT count(*) FROM texto_classificacao"),
    ("item_categoria",    400_000,    "SELECT count(*) FROM item_categoria"),
    ("documento_pagina",  888_656,    "SELECT count(*) FROM documento_pagina"),
    ("item_enriquecido",  302_514,    "SELECT count(*) FROM item_enriquecido"),
    ("par",               250_114,    "SELECT count(*) FROM par"),
    ("label",            250_085,    "SELECT count(*) FROM label"),
    ("embedding_cache",   305_000,    "SELECT count(*) FROM embedding_cache"),
    ("grupo_item",        118_722,    "SELECT count(*) FROM grupo_item"),
)

# Toda contagem abaixo DEVE ser zero.
ORFAOS = (
    ("item sem documento",
     "SELECT count(*) FROM item i "
     "LEFT JOIN documento d USING (numero_controle_pncp) WHERE d IS NULL"),
    ("enriquecido sem item",
     "SELECT count(*) FROM item_enriquecido e LEFT JOIN item i USING (item_key) "
     "WHERE i IS NULL"),
    ("par sem item",
     "SELECT count(*) FROM par p LEFT JOIN item i USING (item_key) WHERE i IS NULL"),
    ("par sem catalogo",
     "SELECT count(*) FROM par p LEFT JOIN catalogo_item c "
     "ON c.tipo = p.tipo AND c.codigo = p.codigo WHERE c IS NULL"),
    ("grupo sem par",
     "SELECT count(*) FROM grupo_item g LEFT JOIN par p USING (par_key) WHERE p IS NULL"),
    ("grupo sem item",
     "SELECT count(*) FROM grupo_item g LEFT JOIN item i USING (item_key) WHERE i IS NULL"),
    ("item sem texto_hash",
     "SELECT count(*) FROM item WHERE texto_hash IS NULL OR texto_hash = ''"),
    ("pagina sem documento",
     "SELECT count(*) FROM documento_pagina p "
     "LEFT JOIN documento d USING (numero_controle_pncp) WHERE d IS NULL"),
    ("item_categoria sem item",
     "SELECT count(*) FROM item_categoria ic LEFT JOIN item i USING (item_key) "
     "WHERE i IS NULL"),
)

# Coerências que não são FK mas seriam erro de lógica de migração.
COERENCIAS = (
    ("par confirmado sem sinal (nem aceito nem sim)",
     "SELECT count(*) FROM par WHERE final_decision = 'confirmado' "
     "AND decisao IS DISTINCT FROM 'aceito' AND veredito IS DISTINCT FROM 'sim'"),
    ("grupo_item com posicao <= 0",
     "SELECT count(*) FROM grupo_item WHERE posicao <= 0"),
    ("enriquecido de item não-sobrevivente",
     "SELECT count(*) FROM item_enriquecido e JOIN item i USING (item_key) "
     "WHERE NOT i.sobrevivente"),
    ("embedding com dimensão fora do padrão",
     "SELECT count(*) FROM embedding_cache WHERE dimension <> 1024"),
)


def validar() -> bool:
    """Imprime o relatório e devolve True se nenhuma checagem obrigatória falhou."""
    ok = True
    with db.session() as s:
        tabela = Table(title="Contagens (divergência é aceitável SE explicada)")
        tabela.add_column("agregado")
        tabela.add_column("esperado", justify="right")
        tabela.add_column("no banco", justify="right")
        tabela.add_column("Δ", justify="right")
        for label, esperado, consulta in CONTAGENS:
            real = s.execute(sql(consulta)).scalar_one()
            delta = real - esperado
            cor = "green" if abs(delta) <= max(1, esperado // 100) else "yellow"
            tabela.add_row(label, f"{esperado:,}".replace(",", "."),
                           f"{real:,}".replace(",", "."),
                           f"[{cor}]{delta:+,}[/]".replace(",", "."))
        console.print(tabela)

        console.print("\n[bold]Integridade referencial (tem de ser ZERO)[/]")
        for label, consulta in ORFAOS + COERENCIAS:
            n = s.execute(sql(consulta)).scalar_one()
            if n:
                ok = False
                console.print(f"  [red]✗ {label}: {n}[/]")
            else:
                console.print(f"  [green]✓[/] {label}")

    console.print(f"\n{'[bold green]Validação OK[/]' if ok else '[bold red]FALHOU[/]'}")
    return ok


def main() -> None:
    console.print(f"[bold cyan]Validação da migração[/]\n  banco: {db.database_url()}")
    sys.exit(0 if validar() else 1)


if __name__ == "__main__":
    main()
