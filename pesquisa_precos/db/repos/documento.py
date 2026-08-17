"""
Repositório de documentos e itens (`documento`, `documento_termo`, `item`, `item_categoria`).

É o agregado mais pesado: 68 mil documentos e 1,6 milhão de itens. Todas as escritas passam por
`COPY` (`db/copia.py`); nada aqui insere linha a linha.

`item.texto_hash` é gravado NA INGESTÃO, por quem chama, usando `core.textos.texto_hash`. Não
é calculado aqui de propósito: quem monta a linha já tem descrição e unidade em mãos, e um
segundo ponto de cálculo é exatamente o risco que docs/08_CONVENCOES.md §5.4 descreve.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copia

COLUNAS_DOC = ("numero_controle_pncp", "tipo_doc", "orgao", "orgao_cnpj", "uf", "ano",
               "data", "data_assinatura", "data_fim_vigencia", "data_atualizacao_pncp",
               "url_pncp", "n_itens")

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
    return copia.copiar(
        conn, "documento", COLUNAS_DOC, linhas,
        conflito=("numero_controle_pncp",),
        atualizar=("orgao", "orgao_cnpj", "uf", "ano", "data", "data_assinatura",
                   "data_fim_vigencia", "data_atualizacao_pncp", "url_pncp", "n_itens"),
    )


def gravar_itens(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Insere itens (ordem de `COLUNAS_ITEM`). `DO NOTHING`: o item do PNCP é imutável.

    Se a API mudar a descrição de um item já coletado, o `texto_hash` mudaria junto e a
    classificação paga viraria órfã. Preferimos manter o que foi coletado — a atualização de
    um documento vem como `data_atualizacao_pncp` nova, e o tratamento disso é decisão de
    etapa, não de repositório.
    """
    return copia.copiar(conn, "item", COLUNAS_ITEM, linhas, conflito=("item_key",))


def ligar_termos(conn: psycopg.Connection,
                 linhas: Sequence[tuple[str, int]]) -> int:
    """N:N documento × termo — substitui a coluna `conceitos_origem` do CSV."""
    return copia.copiar(conn, "documento_termo", ("numero_controle_pncp", "termo_id"),
                        linhas, conflito=("numero_controle_pncp", "termo_id"))


def marcar_sobreviventes(sessao: Session, item_keys: Sequence[str]) -> int:
    """Resultado da etapa 4. Recebe as chaves em lote e usa `unnest` — uma consulta, não N.

    Só marca; NÃO desmarca o que ficou de fora. A etapa 4 recomputa o corpus inteiro, então
    quem quiser um recorte limpo chama `limpar_sobreviventes()` antes, explicitamente.
    """
    if not item_keys:
        return 0
    return sessao.execute(
        text("UPDATE item SET sobrevivente = true "
             "WHERE item_key = ANY(:keys) AND NOT sobrevivente"),
        {"keys": list(item_keys)},
    ).rowcount


def limpar_sobreviventes(sessao: Session) -> int:
    return sessao.execute(
        text("UPDATE item SET sobrevivente = false WHERE sobrevivente")).rowcount


def recontar_sobreviventes_por_documento(sessao: Session) -> int:
    """Recalcula `documento.n_itens_sobreviventes` por SQL puro. Derivada, sempre recomputável."""
    return sessao.execute(text("""
        UPDATE documento d
           SET n_itens_sobreviventes = COALESCE(c.n, 0),
               atualizado_em = now()
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
             "                       atualizado_em = now() "
             "  FROM unnest(CAST(:ncs AS text[]), CAST(:sts AS text[])) AS e(nc, estado) "
             " WHERE d.numero_controle_pncp = e.nc"),
        {"ncs": [nc for nc, _ in estados], "sts": [st for _, st in estados]},
    ).rowcount


def mapa_pasta_para_controle(sessao: Session) -> dict[str, str]:
    """Não existe no banco — a `pasta_arquivos` deliberadamente NÃO foi migrada (ADR-012).

    Fica aqui como marcador: quem precisar do mapa `doc_key(caminho) → numero_controle_pncp`
    (só o m10, que lê `5_pdf_texto.csv`) deve construí-lo do CSV de origem, não do banco.
    """
    raise NotImplementedError(
        "pasta_arquivos não é migrada (ADR-012). O mapa caminho→controle vive no m10, "
        "construído a partir de 2_itens_coletados.csv.")


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "documento": "SELECT count(*) FROM documento",
        "documento_termo": "SELECT count(*) FROM documento_termo",
        "item": "SELECT count(*) FROM item",
        "item_sobrevivente": "SELECT count(*) FROM item WHERE sobrevivente",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
