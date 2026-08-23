"""
Repositório de documentos e itens (`documento`, `documento_termo`, `item`, `item_categoria`).

É o agregado mais pesado: 68 mil documentos e 1,6 milhão de itens. Todas as escritas passam por
`COPY` (`db/copy.py`); nada aqui insere linha a linha.

`item.texto_hash` é gravado NA INGESTÃO, por quem chama, usando `core.text.texto_hash`. Não
é calculado aqui de propósito: quem monta a linha já tem descrição e unidade em mãos, e um
segundo ponto de cálculo é exatamente o risco que docs/08_CONVENCOES.md §5.4 descreve.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS_DOC = ("numero_controle_pncp", "tipo_doc", "orgao", "orgao_cnpj", "uf", "ano",
               "data", "data_assinatura", "data_fim_vigencia", "data_atualizacao_pncp",
               "url_pncp", "numero_sequencial", "numero_sequencial_ata", "n_itens")

COLUNAS_ITEM = ("item_key", "numero_controle_pncp", "numero_item", "descricao_api",
                "unidade", "quantidade", "preco_unitario", "preco_estimado",
                "fornecedor", "data_resultado", "texto_hash")


def gravar_documentos(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert de documentos (ordem de `COLUNAS_DOC`).

    `DO UPDATE` só nos campos que a API pode revisar. `estado` fica FORA: ele é estado de
    processamento nosso (extraído/suspeito/ilegível), e uma recoleta não pode rebaixar um
    documento já processado de volta para 'descoberto' — isso mandaria a etapa 5 reprocessar
    e repagar o LLM.
    """
    return copy.copiar(
        conn, "documento", COLUNAS_DOC, linhas,
        conflito=("numero_controle_pncp",),
        atualizar=("orgao", "orgao_cnpj", "uf", "ano", "data", "data_assinatura",
                   "data_fim_vigencia", "data_atualizacao_pncp", "url_pncp",
                   "numero_sequencial", "numero_sequencial_ata", "n_itens"),
    )


def gravar_itens(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Insere itens (ordem de `COLUNAS_ITEM`). `DO NOTHING`: o item do PNCP é imutável.

    Se a API mudar a descrição de um item já coletado, o `texto_hash` mudaria junto e a
    classificação paga viraria órfã. Preferimos manter o que foi coletado — a atualização de
    um documento vem como `data_atualizacao_pncp` nova, e o tratamento disso é decisão de
    etapa, não de repositório.
    """
    return copy.copiar(conn, "item", COLUNAS_ITEM, linhas, conflito=("item_key",))


def ligar_termos(conn: psycopg.Connection,
                 linhas: Sequence[tuple[str, int]]) -> int:
    """N:N documento × termo — substitui a coluna `conceitos_origem` do CSV."""
    return copy.copiar(conn, "documento_termo", ("numero_controle_pncp", "termo_id"),
                        linhas, conflito=("numero_controle_pncp", "termo_id"))


def marcar_sobreviventes(sessao: Session, item_keys: Sequence[str]) -> int:
    """Resultado da etapa 4. Recebe as chaves em lote e usa `unnest` — uma consulta, não N.

    Só marca; NÃO desmarca o que ficou de fora. A step 4 recomputa o corpus inteiro, então
    quem quiser um recorte limpo chama `limpar_sobreviventes()` antes, explicitamente.
    """
    if not item_keys:
        return 0
    return sessao.execute(
        text("UPDATE item SET sobrevivente = true "
             "WHERE item_key = ANY(:keys) AND NOT sobrevivente"),
        {"keys": list(item_keys)},
    ).rowcount


def marcar_sobreviventes_por_categoria(sessao: Session) -> dict[str, int]:
    """A step 4 inteira, em SQL: sobrevive o item com ao menos UMA categoria de conteúdo.

    O caminho CSV carrega dois arquivos (182 MB de saída), faz merge, explode o multi-label e
    reagrega — tudo para produzir um booleano por item. Aqui é um UPDATE nos dois sentidos,
    porque "sobrevivente" é ATRIBUTO do item, não um conjunto à parte (ADR-018): não existe
    tabela de sobreviventes para ficar fora de sincronia com `item`.

    O `UPDATE` desmarca quem deixou de ter categoria — a etapa 4 sempre recomputa o corpus
    inteiro (o corte depende de tudo que existe), então marcar sem desmarcar deixaria item
    reprovado numa reclassificação marcado para sempre.

    A regra dos 5 (`MIN_ITENS`/`TOP_N`) NÃO entra aqui: está desativada de propósito
    (ADR-016), e a contagem por categoria é só diagnóstico.
    """
    marcados = sessao.execute(text("""
        UPDATE item SET sobrevivente = true
         WHERE NOT sobrevivente
           AND EXISTS (SELECT 1 FROM item_categoria ic WHERE ic.item_key = item.item_key)
    """)).rowcount
    desmarcados = sessao.execute(text("""
        UPDATE item SET sobrevivente = false
         WHERE sobrevivente
           AND NOT EXISTS (SELECT 1 FROM item_categoria ic WHERE ic.item_key = item.item_key)
    """)).rowcount
    return {"marcados": marcados, "desmarcados": desmarcados}


def relatorio_por_categoria(sessao: Session) -> list[dict]:
    """Contagem por categoria — o `4_relatorio_corte.csv`, que era diagnóstico e continua."""
    return [
        {"categoria": c, "n_itens_coletados": n, "mantida": True}
        for c, n in sessao.execute(text(
            "SELECT categoria, count(*) FROM item_categoria "
            "GROUP BY categoria ORDER BY count(*) DESC")).all()
    ]


def limpar_sobreviventes(sessao: Session) -> int:
    return sessao.execute(
        text("UPDATE item SET sobrevivente = false WHERE sobrevivente")).rowcount


def recontar_sobreviventes_por_documento(sessao: Session) -> int:
    """Recalcula `documento.n_itens_sobreviventes` por SQL puro. Derivada, sempre recomputável."""
    return sessao.execute(text("""
        UPDATE documento d
           SET n_itens_sobreviventes = COALESCE(c.n, 0),
               updated_at = now()
          FROM (SELECT numero_controle_pncp, count(*) FILTER (WHERE sobrevivente) AS n
                  FROM item GROUP BY numero_controle_pncp) c
         WHERE c.numero_controle_pncp = d.numero_controle_pncp
           AND d.n_itens_sobreviventes IS DISTINCT FROM COALESCE(c.n, 0)
    """)).rowcount


def atualizar_estado(sessao: Session, estados: Sequence[tuple[str, str]]) -> int:
    """(numero_controle_pncp, estado) em lote, via `unnest` de dois arrays paralelos."""
    if not estados:
        return 0
    return sessao.execute(
        text("UPDATE documento d SET estado = CAST(e.estado AS estado_documento), "
             "                       updated_at = now() "
             "  FROM unnest(CAST(:ncs AS text[]), CAST(:sts AS text[])) AS e(nc, estado) "
             " WHERE d.numero_controle_pncp = e.nc"),
        {"ncs": [nc for nc, _ in estados], "sts": [st for _, st in estados]},
    ).rowcount


def mapa_pasta_para_controle(sessao: Session) -> dict[str, str]:
    """Não existe no banco — a `pasta_arquivos` deliberadamente NÃO foi migrada (ADR-012).

    Fica aqui como marcador: quem precisar do mapa `doc_key(caminho) → numero_controle_pncp`
    (só o m10, que lê `5_pdf_texto.csv`) deve construí-lo do CSV de source, não do banco.
    """
    raise NotImplementedError(
        "pasta_arquivos não é migrada (ADR-012). O mapa caminho→controle vive no m10, "
        "construído a partir de 2_itens_coletados.csv.")


# ── Coleta da etapa 2 no banco (Fase 10) ────────────────────────────────────────────

def buscas_concluidas(sessao: Session) -> set[tuple[int, str]]:
    """(termo_id, tipo_doc) já varridos — o `ler_chaves_concluidas(2_progresso.csv)`."""
    return {(tid, td) for tid, td in sessao.execute(
        text("SELECT termo_id, tipo_doc::text FROM coleta_progresso")).all()}


def marcar_busca(sessao: Session, termo_id: int, tipo_doc: str,
                 n_documentos: int = 0, n_itens: int = 0) -> None:
    """Fecha uma busca (termo × fonte). Os contadores são acumulados, não substituídos: uma
    revarredura do mesmo termo soma o que trouxe a mais em vez de zerar o histórico."""
    sessao.execute(text("""
        INSERT INTO coleta_progresso (termo_id, tipo_doc, n_documentos, n_itens)
        VALUES (:id, CAST(:td AS tipo_documento), :nd, :ni)
        ON CONFLICT (termo_id, tipo_doc) DO UPDATE
           SET n_documentos = coleta_progresso.n_documentos + EXCLUDED.n_documentos,
               n_itens = coleta_progresso.n_itens + EXCLUDED.n_itens,
               finished_at = now()
    """), {"id": termo_id, "td": tipo_doc, "nd": n_documentos, "ni": n_itens})


def limpar_progresso(sessao: Session) -> int:
    """`--ignorar-cache`: refaz todas as buscas. Não apaga documento nem item — o upsert
    absorve a repetição, e apagar jogaria fora classificação já paga."""
    return sessao.execute(text("DELETE FROM coleta_progresso")).rowcount


def controles_conhecidos(sessao: Session) -> set[str]:
    """Todo `numeroControlePNCP` já visto — o `indexar_docs_existentes()` do caminho CSV.

    O dedup por documento é o que segura o custo de todas as etapas abaixo (um documento
    aparece em dezenas de buscas). No CSV era preciso reconstruir doc→[item_key] para ligar
    os conceitos extras; aqui não: `documento_termo` é por documento, não por item.
    """
    return set(sessao.scalars(text("SELECT numero_controle_pncp FROM documento")).all())


def pendentes(sessao: Session) -> dict[str, dict]:
    """Documentos sem resultado homologado, no formato que a etapa consome."""
    return {
        nc: {"tipo_doc": td, "termo_id": tid, "motivo": motivo, "data": data, "base": base}
        for nc, td, tid, motivo, data, base in sessao.execute(text(
            "SELECT numero_controle_pncp, tipo_doc::text, termo_id, motivo, data, base "
            "  FROM coleta_pendente")).all()
    }


def gravar_pendente(sessao: Session, numero_controle: str, tipo_doc: str, base: dict, *,
                    termo_id: int | None = None, motivo: str = "sem_homologado",
                    data: str | None = None) -> None:
    import json

    sessao.execute(text("""
        INSERT INTO coleta_pendente (numero_controle_pncp, tipo_doc, termo_id, motivo, data, base)
        VALUES (:nc, CAST(:td AS tipo_documento), :tid, :motivo, :data, CAST(:base AS jsonb))
        ON CONFLICT (numero_controle_pncp) DO UPDATE
           SET motivo = EXCLUDED.motivo, base = EXCLUDED.base, visto_em = now()
    """), {"nc": numero_controle, "td": tipo_doc, "tid": termo_id, "motivo": motivo,
           "data": data, "base": json.dumps(base, ensure_ascii=False, default=str)})


def remover_pendente(sessao: Session, numero_controle: str) -> int:
    """A homologação saiu: o documento vira item coletado e sai da fila de revisita."""
    return sessao.execute(
        text("DELETE FROM coleta_pendente WHERE numero_controle_pncp = :nc"),
        {"nc": numero_controle}).rowcount


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "documento": "SELECT count(*) FROM documento",
        "documento_termo": "SELECT count(*) FROM documento_termo",
        "item": "SELECT count(*) FROM item",
        "item_sobrevivente": "SELECT count(*) FROM item WHERE sobrevivente",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
