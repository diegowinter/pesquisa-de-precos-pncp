"""
m08 — Classificação: `3_itens_classificados.csv` → `texto_classificacao` + `item_categoria`.

O CSV é por `item_key` (1,6 milhão de linhas); o destino é por TEXTO (~320 mil). O colapso é
o ponto central deste passo — e é feito em SQL, não em Python: carregar `item_key → texto_hash`
num dict custaria centenas de MB e refaria um join que o banco já sabe fazer.

O caminho é: `COPY` do CSV para uma tabela de apoio → join com `item` (que já traz o
`texto_hash` gravado pelo m07) → agrupamento por `texto_hash`, vencendo a classificação mais
frequente. Divergência dentro de um mesmo texto **não é silenciada**: ela indica que o dedup da
v2/v3 não era perfeito, e o número aparece no relatório (docs/05_MIGRACAO.md §m08).

`confianca` no CSV é uma PALAVRA ('alta'/'baixa'/'erro'), não um número; a coluna destino é
`real`. A conversão é uma escala ORDINAL declarada em `CONFIANCA` — não é probabilidade, e
tratá-la como tal seria inventar precisão que o dado nunca teve. 'erro' vira NULL, porque
aquela linha não é uma classificação: é a marca de uma chamada que falhou.

`model`/`provider` são constantes: o acervo foi classificado pelo model local (LM Studio) ao
longo da v2/v3 e não há registro por linha de qual build era. Ficam sobrescrevíveis por flag em
vez de gravados como um palpite fixo.

Uso: python -m migracao.m08_classificacao [--model X] [--provider Y] [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from sqlalchemy import text as sql

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import copiar, em_lotes
from pesquisa_precos.db.repos import classification as repo
from pesquisa_precos.db.repos import execution as repo_exec
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    existe,
    ler_csv,
    lista_pipe,
)

LOTE = 20_000
STAGING = "stg_m08_classificacao"

# Escala ORDINAL, não probabilidade. Preserva a ordem que o rótulo textual carregava e nada
# além disso; qualquer leitura como "72% de certeza" seria leitura errada.
# A escala vive em `db.repos.classification` (fonte única — a etapa 3 usa a MESMA).
CONFIANCA = repo.CONFIANCA_ORDINAL

MODELO_PADRAO = "acervo v2/v3 (model local, build não registrado)"
PROVEDOR_PADRAO = "lm_studio"


def preparar_staging(conn) -> None:
    """Tabela de apoio UNLOGGED: ela existe por minutos e não precisa sobreviver a um crash
    (o CSV de source é intocado e a retomada recomeça o lote). UNLOGGED evita escrever 1,6
    milhão de linhas no WAL à toa."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE UNLOGGED TABLE IF NOT EXISTS {STAGING} (
                item_key   text PRIMARY KEY,
                categorias text[] NOT NULL DEFAULT '{{}}',
                confianca  real
            )
        """)


def carregar_staging(rel: Relatorio, retomada: Retomada, total: int) -> None:
    def linhas():
        for i, r in enumerate(ler_csv(paths.E3_CLASSIFICADOS), 1):
            if i <= retomada.linhas:
                continue
            item_key = (r.get("item_key") or "").strip()
            if not item_key:
                rel.mais("linhas sem item_key")
                continue
            rel.mais("linhas lidas")
            yield (item_key, lista_pipe(r.get("categorias", "")),
                   CONFIANCA.get((r.get("confianca") or "").strip().lower()))

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), TimeRemainingColumn(),
                  console=console) as barra, db.raw_connection() as conn:
        preparar_staging(conn)
        conn.commit()
        tarefa = barra.add_task("carregando classificações", total=total,
                                completed=retomada.linhas)
        for lote in em_lotes(linhas(), LOTE):
            copiar(conn, STAGING, ("item_key", "categorias", "confianca"), lote,
                   conflito=("item_key",), atualizar=("categorias", "confianca"))
            conn.commit()
            retomada.avancar(len(lote))
            barra.update(tarefa, completed=retomada.linhas)


def colapsar_por_texto(rel: Relatorio, model: str, provider: str) -> None:
    """Agrupa por `texto_hash` e insere o vencedor por frequência."""
    with db.session() as s:
        prompt_version_id = repo_exec.prompt_versao_ativa(s, "classificar_item")
        run_id = repo_exec.run_do_acervo_migrado(s)

        divergentes = s.execute(sql(f"""
            SELECT count(*) FROM (
                SELECT i.texto_hash
                  FROM {STAGING} s JOIN item i USING (item_key)
                 GROUP BY i.texto_hash
                HAVING count(DISTINCT s.categorias) > 1
            ) d
        """)).scalar_one()
        if divergentes:
            rel.aviso(f"{divergentes} textos receberam classificações DIFERENTES entre seus "
                      f"itens na v2/v3 — venceu a mais frequente. Sinal de que o dedup antigo "
                      f"não era perfeito; nenhum dado foi perdido, mas vale saber.")
        rel.mais("textos com divergência", divergentes)

        # `DISTINCT ON` com ORDER BY (texto_hash, n DESC, categorias) = moda, com desempate
        # determinístico pela própria lista de categorias — para que rodar duas vezes dê o
        # mesmo resultado, que é o requisito de idempotência.
        inseridos = s.execute(sql(f"""
            INSERT INTO texto_classificacao
                (texto_hash, description, unidade, categorias, confianca,
                 prompt_version_id, model, provider, run_id)
            SELECT texto_hash, description, unidade, categorias, confianca,
                   :pv, :model, :provider, :run
              FROM (
                SELECT DISTINCT ON (texto_hash) *
                  FROM (
                    SELECT i.texto_hash,
                           min(i.descricao_api) AS description,
                           min(i.unidade)       AS unidade,
                           s.categorias,
                           max(s.confianca)     AS confianca,
                           count(*)             AS n
                      FROM {STAGING} s JOIN item i USING (item_key)
                     GROUP BY i.texto_hash, s.categorias
                  ) contagem
                 ORDER BY texto_hash, n DESC, categorias
              ) vencedor
            ON CONFLICT (texto_hash) DO NOTHING
        """), {"pv": prompt_version_id, "model": model, "provider": provider,
               "run": run_id}).rowcount
        rel.mais("texto_classificacao inseridos", inseridos)

        rel.mais("item_categoria inseridos", repo.recomputar_item_categoria(s))
        for key, value in repo.contar(s).items():
            rel.mais(f"{key} no banco", value)


def descartar_staging() -> None:
    with db.raw_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {STAGING}")


def migrar(model: str = MODELO_PADRAO, provider: str = PROVEDOR_PADRAO,
           reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m08 — classificação")
    if not existe(paths.E3_CLASSIFICADOS):
        raise SystemExit(f"{paths.E3_CLASSIFICADOS} ausente. Rode a step 3 antes.")

    retomada = Retomada.carregar("m08_classificacao")
    if reiniciar:
        retomada.zerar()
        descartar_staging()

    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E3_CLASSIFICADOS)
    rel.mais("registros no CSV (estimado)", total)

    carregar_staging(rel, retomada, total)
    colapsar_por_texto(rel, model, provider)
    descartar_staging()
    retomada.zerar()  # staging consumida: uma reexecução recomeça o carregamento do zero
    return rel


def main() -> None:
    cabecalho("m08 — classificação", paths.E3_CLASSIFICADOS,
              "texto_classificacao, item_categoria")
    console.print(f"  banco  : {db.database_url()}")
    args = sys.argv[1:]

    def flag(name: str, default: str) -> str:
        return args[args.index(name) + 1] if name in args else default

    migrar(model=flag("--model", MODELO_PADRAO),
           provider=flag("--provider", PROVEDOR_PADRAO),
           reiniciar="--reiniciar" in args).imprimir()


if __name__ == "__main__":
    main()
