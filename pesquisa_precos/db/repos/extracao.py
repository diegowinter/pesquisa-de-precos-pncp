"""
Repositório da etapa 5 (`documento_pagina`, `documento_extracao`, `item_enriquecido`).

`item_enriquecido` é o CONTRATO DE SAÍDA da etapa 5 (ADR-010): as etapas 6, 7 e 8 leem só ela
e ignoram por qual estratégia o dado chegou. Por isso as funções de leitura daqui nunca
expõem `estrategia` como filtro obrigatório.

`documento_pagina` é o gigante do banco (888 mil linhas, 2,6 GB de texto em CSV). Lote de
`COPY` menor aqui — cada linha carrega uma página inteira.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copia

LOTE_PAGINA = 1_000  # docs/05_MIGRACAO.md §m10 — linhas grandes

COLUNAS_PAGINA = ("numero_controle_pncp", "arquivo", "pagina", "fonte", "texto")

COLUNAS_EXTRACAO = ("numero_controle_pncp", "estrategia", "itens_json", "n_paginas",
                    "n_paginas_ocr", "tokens_in", "tokens_out", "custo_usd",
                    "duracao_ms", "modelo", "provedor", "run_id")

COLUNAS_ENRIQUECIDO = ("item_key", "descricao_final", "fonte_descricao", "preco_api",
                       "preco_pdf", "divergencia_preco", "fornecedor", "quantidade_pdf",
                       "status", "destino", "estrategia", "doc_status", "run_id")


def gravar_paginas(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Texto por página (ordem de `COLUNAS_PAGINA`). `n_chars` é coluna gerada — não enviar."""
    return copia.copiar(conn, "documento_pagina", COLUNAS_PAGINA, linhas,
                        conflito=("numero_controle_pncp", "arquivo", "pagina"))


def gravar_extracoes(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Uma linha por (documento, estratégia) — é o que permite comparar as duas rotas de
    extração sobre o mesmo documento sem perder o resultado anterior (ADR-010)."""
    return copia.copiar(
        conn, "documento_extracao", COLUNAS_EXTRACAO, linhas,
        conflito=("numero_controle_pncp", "estrategia"),
        atualizar=("itens_json", "n_paginas", "n_paginas_ocr", "tokens_in", "tokens_out",
                   "custo_usd", "duracao_ms", "modelo", "provedor"),
    )


def gravar_enriquecidos(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Contrato de saída da etapa 5 (ordem de `COLUNAS_ENRIQUECIDO`).

    `DO UPDATE`: reprocessar um documento por outra estratégia DEVE sobrescrever o veredito do
    item — é a ação "reprocessar este documento com outra estratégia" da interface. O
    resultado anterior não se perde: ele continua em `documento_extracao`, por estratégia.
    """
    return copia.copiar(
        conn, "item_enriquecido", COLUNAS_ENRIQUECIDO, linhas,
        conflito=("item_key",),
        atualizar=("descricao_final", "fonte_descricao", "preco_api", "preco_pdf",
                   "divergencia_preco", "fornecedor", "quantidade_pdf", "status",
                   "destino", "estrategia", "doc_status", "run_id"),
    )


def descricao_final_por_item(sessao: Session) -> dict[str, str]:
    """`item_key → descricao_final`, caindo para a descrição da API quando não há enriquecido.

    É o `core.textos.descricao_itens()` em SQL: mesma regra (PDF quando houver, senão API),
    mas sem carregar dois CSVs de 190 MB em memória.
    """
    linhas = sessao.execute(text("""
        SELECT i.item_key, COALESCE(NULLIF(e.descricao_final, ''), i.descricao_api)
        FROM item i
        LEFT JOIN item_enriquecido e USING (item_key)
        WHERE i.sobrevivente
    """)).all()
    return dict(linhas)


def item_keys_por_destino(sessao: Session, destino: str = "manter") -> set[str]:
    """Comporta de enriquecimento da etapa 6a: só 'manter' segue para o pareamento."""
    return set(sessao.scalars(
        text("SELECT item_key FROM item_enriquecido "
             "WHERE destino = CAST(:d AS destino_item)"), {"d": destino}).all())


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "documento_pagina": "SELECT count(*) FROM documento_pagina",
        "documento_extracao": "SELECT count(*) FROM documento_extracao",
        "item_enriquecido": "SELECT count(*) FROM item_enriquecido",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
