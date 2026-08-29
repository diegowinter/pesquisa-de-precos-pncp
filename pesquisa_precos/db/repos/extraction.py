"""
Repositório da etapa 5 (`documento_extracao`, `item_enriquecido`).

`item_enriquecido` é o CONTRATO DE SAÍDA da etapa 5: as etapas 6, 7 e 8 leem só ela, e dela
só `descricao_final` e `destino`. Foi o que permitiu trocar a extração inteira (ADR-023) sem
tocar em nenhuma etapa a jusante.

`documento_extracao` guarda a tabela de itens que o modelo leu do documento, em texto livre —
uma linha por documento. Ela substituiu `documento_pagina`, que transcrevia o documento
inteiro página a página e não era lida por ninguém.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS_EXTRACAO = ("numero_controle_pncp", "tabela_texto", "n_paginas", "tokens_in",
                    "tokens_out", "cost_usd", "duration_ms", "model", "provider", "run_id")

COLUNAS_ENRIQUECIDO = ("item_key", "descricao_final", "fonte_descricao", "preco_api",
                       "preco_pdf", "divergencia_preco", "fornecedor", "quantidade_pdf",
                       "status", "destino", "doc_status", "run_id")


def gravar_extracoes(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Uma linha por documento. Reextrair sobrescreve: não há mais duas rotas para comparar
    sobre o mesmo documento, e guardar a tabela anterior só deixaria dúvida sobre qual vale."""
    return copy.copiar(
        conn, "documento_extracao", COLUNAS_EXTRACAO, linhas,
        conflito=("numero_controle_pncp",),
        atualizar=("tabela_texto", "n_paginas", "tokens_in", "tokens_out",
                   "cost_usd", "duration_ms", "model", "provider", "run_id"),
    )


def gravar_enriquecidos(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Contrato de saída da etapa 5 (ordem de `COLUNAS_ENRIQUECIDO`).

    `DO UPDATE`: reprocessar um documento DEVE sobrescrever o veredito de todos os seus itens
    — é a ação "reprocessar este documento" da interface.
    """
    return copy.copiar(
        conn, "item_enriquecido", COLUNAS_ENRIQUECIDO, linhas,
        conflito=("item_key",),
        atualizar=("descricao_final", "fonte_descricao", "preco_api", "preco_pdf",
                   "divergencia_preco", "fornecedor", "quantidade_pdf", "status",
                   "destino", "doc_status", "run_id"),
    )


def contar(sessao: Session) -> dict[str, int]:
    q = {
        "documento_extracao": "SELECT count(*) FROM documento_extracao",
        "item_enriquecido": "SELECT count(*) FROM item_enriquecido",
    }
    return {k: sessao.execute(text(v)).scalar_one() for k, v in q.items()}
