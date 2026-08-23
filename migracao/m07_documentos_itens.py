"""
m07 — Documentos e itens: `2_itens_coletados.csv` → `documento`, `documento_termo`, `item`.

O passo mais pesado da migração: 746 MB, 1.613.517 linhas, achatado (o documento se repete a
cada item). Duas passadas em STREAMING, nunca `pd.read_csv` (docs/08_CONVENCOES.md §5.2).

  Passada 1 — documentos. Agrupa por `numeroControlePNCP`, pega os campos de documento da
              PRIMEIRA ocorrência, conta itens e reúne os conceitos de origem. 68 mil
              documentos cabem em memória; 1,6 milhão de itens não caberiam, e por isso a
              passada 2 é separada.
  Passada 2 — itens. Uma linha por item, com o `texto_hash` calculado AQUI.

`texto_hash = core.text.texto_hash(descricao_api, unidade)`. É a mesma função que a etapa 3
usa para agrupar. Uma diferença mínima entre as duas pontas invalidaria o dedup permanente e
mandaria 320 mil textos já pagos de volta ao LLM (docs/08_CONVENCOES.md §5.4).

`conceitos_origem` → `documento_termo`, consolidando também `checkpoints/2_conceitos_extra.csv`
(os conceitos acrescentados por dedup de documento). É a mesma consolidação que
`collect_pncp.carregar_itens_coletados()` faz em memória hoje — replicada aqui em streaming,
porque aquela função carrega o CSV inteiro.

`pasta_arquivos` **não é migrada como caminho** (ADR-012). No lugar vai `url_pncp`,
reconstruída por `core.collection.urls.url_documento` — é o que torna o descarte do PDF reversível.

`data_atualizacao_pncp` não existe no CSV da v2: fica NULL. O watermark vem do m06.

DIFERENÇA CONHECIDA E ACEITA no export: os campos de texto entram no banco com `strip()`. O
PNCP devolve unidades com espaço à direita ("Unidade  "), e o CSV as guardava assim. Medido
numa amostra de 8.154 linhas de export, é a ÚNICA célula que difere entre o caminho CSV e o
caminho banco — 473 linhas, só na coluna "Unidade", e só por espaço em branco. O `texto_hash`
já colapsa espaço, então o dedup de classificação não muda. Se a fidelidade byte a byte com o
último export oficial for mais importante que a normalização, o ponto a mudar é o `txt()` de
`_comum.py`, não este arquivo.

Uso: python -m migracao.m07_documentos_itens [--reiniciar]
"""

import sys

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from pesquisa_precos.config import paths
from pesquisa_precos.core.collection.urls import url_documento
from pesquisa_precos.core.text import normalizar_termo, texto_hash
from pesquisa_precos.db import session as db
from pesquisa_precos.db.copy import em_lotes
from pesquisa_precos.db.repos import documento as repo
from pesquisa_precos.db.repos import termo as repo_termo
from migracao._comum import (
    Relatorio,
    Retomada,
    cabecalho,
    console,
    estimar_linhas,
    data,
    dec,
    existe,
    inteiro,
    ler_csv,
    lista_pipe,
    txt,
)

LOTE = 5_000


def _barra() -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeRemainingColumn(),
        console=console, transient=False)


def conceitos_extra() -> dict[str, set[str]]:
    """`numero_controle_pncp → {conceitos}` a partir do checkpoint de conceitos extras.

    O checkpoint é por `item_key`, mas a relação real é documento × termo — e `item_key` é
    `<numero_controle>::<numero_item>`, então o documento sai do próprio prefixo.
    """
    fora: dict[str, set[str]] = {}
    if not existe(paths.CK_2_CONCEITOS_EXTRA):
        return fora
    for r in ler_csv(paths.CK_2_CONCEITOS_EXTRA):
        item_key = (r.get("item_key") or "").strip()
        conceito = (r.get("conceito") or "").strip()
        if item_key and conceito:
            fora.setdefault(item_key.split("::", 1)[0], set()).add(conceito)
    return fora


def passada_documentos(rel: Relatorio, total: int) -> dict[str, set[str]]:
    """Grava `documento` e devolve `numero_controle_pncp → {conceitos}` para a ligação."""
    docs: dict[str, list] = {}
    conceitos: dict[str, set[str]] = {}

    with _barra() as barra:
        tarefa = barra.add_task("passada 1/2 · documentos", total=total)
        for i, r in enumerate(ler_csv(paths.E2_ITENS), 1):
            nc = (r.get("numeroControlePNCP") or "").strip()
            if not nc:
                rel.mais("linhas sem numeroControlePNCP")
                continue
            tipo_doc = (r.get("tipo_doc") or "").strip()
            if nc not in docs:
                docs[nc] = [
                    nc, tipo_doc, txt(r.get("orgao")), txt(r.get("orgao_cnpj")),
                    txt(r.get("uf")), inteiro(r.get("ano")), data(r.get("data")),
                    data(r.get("data_assinatura")), data(r.get("data_fim_vigencia")),
                    None,                                # data_atualizacao_pncp: não existe na v2
                    url_documento(nc, tipo_doc) or None,
                    # Só as linhas coletadas a partir da Fase 8 trazem os sequenciais; as
                    # herdadas da v2 vêm vazias, e a etapa 5 cai no fluxo de rebaixar por
                    # `url_pncp`. Não é perda nova — a v2 nunca gravou esses campos.
                    txt(r.get("numero_sequencial")),
                    txt(r.get("numero_sequencial_ata")),
                    0,                                   # n_itens: contado abaixo
                ]
            docs[nc][-1] += 1
            conceitos.setdefault(nc, set()).update(lista_pipe(r.get("conceitos_origem", "")))
            if i % 20_000 == 0:
                barra.update(tarefa, completed=i)
        barra.update(tarefa, completed=total)

    rel.mais("documentos distintos", len(docs))
    sem_url = sum(1 for d in docs.values() if not d[10])
    if sem_url:
        rel.aviso(f"{sem_url} documentos ficaram sem url_pncp (número de controle fora do "
                  f"formato conhecido) — eles não podem ser rebaixados do PNCP.")

    with db.raw_connection() as conn:
        for lote in em_lotes(docs.values(), LOTE):
            rel.mais("documentos gravados", repo.gravar_documentos(conn, lote))

    # Conceitos extras entram DEPOIS, para que um documento presente só no checkpoint (sem
    # linha no CSV principal) não crie uma ligação órfã.
    for nc, extras in conceitos_extra().items():
        if nc in conceitos:
            conceitos[nc].update(extras)
            rel.mais("conceitos extras aplicados", len(extras))
        else:
            rel.mais("conceitos extras de documento desconhecido")
    return conceitos


def ligar_termos(rel: Relatorio, conceitos: dict[str, set[str]]) -> None:
    with db.session() as s:
        por_norm = repo_termo.id_por_norm(s)

    def pares():
        for nc, nomes in conceitos.items():
            for nome in nomes:
                termo_id = por_norm.get(normalizar_termo(nome))
                if termo_id is None:
                    rel.mais("conceitos sem termo correspondente")
                    continue
                yield (nc, termo_id)

    with db.raw_connection() as conn:
        for lote in em_lotes(pares(), LOTE):
            rel.mais("documento_termo gravados", repo.ligar_termos(conn, lote))


def passada_itens(rel: Relatorio, total: int, retomada: Retomada) -> None:
    pular = retomada.linhas
    if pular:
        console.print(f"  [dim]retomando: pulando {pular:,} linhas já processadas[/]"
                      .replace(",", "."))

    def linhas():
        for i, r in enumerate(ler_csv(paths.E2_ITENS), 1):
            if i <= pular:
                continue
            item_key = (r.get("item_key") or "").strip()
            nc = (r.get("numeroControlePNCP") or "").strip()
            numero = inteiro(r.get("numeroItem"))
            if not (item_key and nc and numero is not None):
                rel.mais("itens sem chave (descartados)")
                continue
            descricao = r.get("descricao_api") or ""
            unidade = txt(r.get("unidade"))
            rel.mais("itens lidos")
            yield (item_key, nc, numero, descricao, unidade,
                   dec(r.get("quantidade")), dec(r.get("preco_unitario")),
                   dec(r.get("preco_estimado")), txt(r.get("fornecedor")),
                   data(r.get("data_resultado")),
                   texto_hash(descricao, unidade))

    with _barra() as barra, db.raw_connection() as conn:
        tarefa = barra.add_task("passada 2/2 · itens", total=total, completed=pular)
        # O `COPY` de cada lote e o avanço da retomada acontecem na MESMA transação da
        # conexão bruta? Não: `raw_connection` só comita no fim. Por isso o commit é por lote,
        # explícito — sem ele, uma interrupção perderia tudo e a retomada mentiria.
        for lote in em_lotes(linhas(), LOTE):
            repo.gravar_itens(conn, lote)
            conn.commit()
            retomada.avancar(len(lote))
            rel.mais("itens gravados", len(lote))
            barra.update(tarefa, completed=retomada.linhas)


def migrar(reiniciar: bool = False) -> Relatorio:
    rel = Relatorio("m07 — documentos e itens")
    if not existe(paths.E2_ITENS):
        raise SystemExit(f"{paths.E2_ITENS} ausente. Rode a etapa 2 antes.")

    retomada = Retomada.carregar("m07_itens")
    if reiniciar:
        retomada.zerar()

    console.print("  contando linhas do CSV…")
    total = estimar_linhas(paths.E2_ITENS)
    rel.mais("registros no CSV (estimado)", total)

    # A passada 1 é barata de refazer (só documentos) e precisa rodar sempre: é ela que
    # garante que a FK `item.numero_controle_pncp` encontre o documento na passada 2.
    conceitos = passada_documentos(rel, total)
    ligar_termos(rel, conceitos)
    passada_itens(rel, total, retomada)

    with db.session() as s:
        for chave, valor in repo.contar(s).items():
            rel.mais(f"{chave} no banco", valor)
    return rel


def main() -> None:
    cabecalho("m07 — documentos e itens",
              [paths.E2_ITENS, paths.CK_2_CONCEITOS_EXTRA],
              "documento, documento_termo, item")
    console.print(f"  banco  : {db.database_url()}")
    migrar(reiniciar="--reiniciar" in sys.argv).imprimir()


if __name__ == "__main__":
    main()
