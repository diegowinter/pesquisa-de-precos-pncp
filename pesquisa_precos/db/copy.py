"""
Ingestão em massa via `COPY` + tabela temporária.

Por que não `session.bulk_insert_mappings`: o acervo tem 1,6 milhão de itens e 888 mil páginas
de texto. Um INSERT por linha (ou até em lotes de mil) leva horas; `COPY` leva minutos. Como
`COPY` não sabe resolver conflito, o padrão aqui é o clássico em três tempos:

    1. `CREATE TEMP TABLE tmp (LIKE destino INCLUDING DEFAULTS) ON COMMIT DROP`
    2. `COPY tmp (colunas) FROM STDIN`
    3. `INSERT INTO destino SELECT ... FROM tmp ON CONFLICT ... DO NOTHING|DO UPDATE`

O passo 3 é o que dá a **idempotência** exigida por docs/05_MIGRACAO.md §1: rodar duas vezes
não duplica. E como a temp é `ON COMMIT DROP`, um lote interrompido no meio não deixa lixo.

Regra: a tabela temporária é deduplicada antes do INSERT (`DISTINCT ON` na chave de conflito).
Sem isso, um lote que traga a MESMA chave duas vezes faz o Postgres levantar
"ON CONFLICT DO UPDATE command cannot affect row a second time" — e o CSV do acervo tem
duplicatas de verdade (a etapa 2 é append-only).
"""

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import psycopg

# Lote padrão. 5.000 é o número de docs/05_MIGRACAO.md §3 (m07); `documento_pagina` usa 1.000
# porque cada linha carrega uma página inteira de texto.
LOTE_PADRAO = 5_000


def texto_para_pg(valor: str) -> str:
    """Remove bytes NUL (0x00), que uma coluna `text` do PostgreSQL não aceita.

    BUG REAL, encontrado ao migrar `5_pdf_texto.csv`: o parser de PDF deixa passar NUL em
    páginas de certos documentos, e o `COPY` morre com
    `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes` — abortando o
    lote inteiro. Num arquivo de 2,6 GB isso significa descobrir o problema depois de meia
    hora de migração, e de novo a cada retomada.

    Descartar o NUL é seguro: ele não representa caractere nenhum no texto extraído, é ruído
    de parser. O que NÃO seria seguro é escondê-lo dentro de `copiar()` para todas as colunas —
    aí um NUL vindo de outro lugar (onde ele indicaria dado corrompido de verdade) sumiria sem
    ninguém ver. Por isso a limpeza é explícita, chamada por quem lida com texto de PDF.
    """
    return valor.replace("\x00", "") if "\x00" in valor else valor


def em_lotes(itens: Iterable[Any], tamanho: int = LOTE_PADRAO) -> Iterator[list]:
    """Fatia um iterável em listas de `tamanho`. Streaming — nunca materializa tudo."""
    lote: list = []
    for item in itens:
        lote.append(item)
        if len(lote) >= tamanho:
            yield lote
            lote = []
    if lote:
        yield lote


def copiar(
    conn: psycopg.Connection,
    tabela: str,
    colunas: Sequence[str],
    linhas: Iterable[Sequence[Any]],
    *,
    conflito: Sequence[str] | None = None,
    atualizar: Sequence[str] | None = None,
    where_update: str | None = None,
) -> int:
    """Copia `linhas` para `tabela`, resolvendo conflito. Devolve quantas linhas foram enviadas.

    `conflito`   colunas da chave de conflito. `None` = INSERT direto (destino vazio/append).
    `atualizar`  colunas a sobrescrever em `DO UPDATE`. `None`/vazio = `DO NOTHING`.
    `where_update` condição extra do `DO UPDATE` (ex.: só sobrescrever se o valor mudou).

    O retorno é a contagem de linhas ENVIADAS, não de linhas efetivamente inseridas — a
    diferença entre as duas é justamente o que a idempotência absorve, e conferir o total real
    é papel da validação por agregado (docs/05_MIGRACAO.md §4), com `count(*)` no destino.
    """
    linhas = list(linhas)
    if not linhas:
        return 0

    lista_cols = ", ".join(colunas)
    tmp = f"tmp_{tabela}"
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE {tmp} (LIKE {tabela} INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy(f"COPY {tmp} ({lista_cols}) FROM STDIN") as copy:
            for linha in linhas:
                copy.write_row(linha)

        if conflito:
            # DISTINCT ON dedupa dentro do próprio lote (ver docstring do módulo).
            chave = ", ".join(conflito)
            origem = f"(SELECT DISTINCT ON ({chave}) {lista_cols} FROM {tmp}) t"
        else:
            origem = f"{tmp} t"

        sql = f"INSERT INTO {tabela} ({lista_cols}) SELECT {lista_cols} FROM {origem}"
        if conflito:
            alvo = ", ".join(conflito)
            if atualizar:
                sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in atualizar)
                sql += f" ON CONFLICT ({alvo}) DO UPDATE SET {sets}"
                if where_update:
                    sql += f" WHERE {where_update}"
            else:
                sql += f" ON CONFLICT ({alvo}) DO NOTHING"
        cur.execute(sql)
        # A temp é ON COMMIT DROP, mas um mesmo lote pode chamar `copiar` várias vezes para a
        # mesma tabela dentro da MESMA transação — sem o drop explícito, a segunda chamada
        # falharia com "relation already exists".
        cur.execute(f"DROP TABLE IF EXISTS {tmp}")
    return len(linhas)
