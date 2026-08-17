"""
Repositório de termos de busca (`termo`, `termo_codigo`, `coleta_watermark`).

`termo_norm` é a chave de dedup (UNIQUE) e vem de `core.textos.normalizar_termo`, que
**preserva o acento** — ao contrário de `normalizar_texto`, usada no `texto_hash`. Não é
inconsistência: "ambulancia" e "ambulância" são duas buscas diferentes no PNCP e a etapa 1 as
gera de propósito. A justificativa completa está na docstring de `normalizar_termo`.
"""

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.core.textos import normalizar_termo


def upsert(sessao: Session, termo_txt: str, categoria: str | None,
           origem: str | None) -> int | None:
    """Insere (ou reaproveita) o termo e devolve seu id. `None` se o texto for vazio.

    `ON CONFLICT DO UPDATE` com um `SET` inócuo em vez de `DO NOTHING`: só o UPDATE faz o
    Postgres devolver a linha no `RETURNING` quando ela já existia. Com `DO NOTHING`, o
    `RETURNING` vem vazio e seria preciso um SELECT extra por termo (499 round-trips).
    """
    norm = normalizar_termo(termo_txt)
    if not norm:
        return None
    return sessao.execute(
        text("INSERT INTO termo (termo, termo_norm, categoria, origem) "
             "VALUES (:t, :n, :c, :o) "
             "ON CONFLICT (termo_norm) DO UPDATE SET termo = termo.termo "
             "RETURNING id"),
        {"t": termo_txt, "n": norm, "c": categoria or None, "o": origem or None},
    ).scalar_one()


def ligar_codigos(sessao: Session, termo_id: int,
                  codigos: Sequence[tuple[str, str]]) -> int:
    """N:N termo × (tipo, codigo). Códigos fora do catálogo são ignorados pelo próprio banco?

    Não — a FK os rejeitaria com erro. Por isso o INSERT filtra por `EXISTS`: o CSV de termos
    lista códigos que já saíram do catálogo filtrado, e derrubar a migração por causa deles
    seria trocar um dado perdido por nenhum dado.
    """
    if not codigos:
        return 0
    n = 0
    for tipo, codigo in codigos:
        n += sessao.execute(
            text("INSERT INTO termo_codigo (termo_id, tipo, codigo) "
                 "SELECT :id, CAST(:t AS tipo_catalogo), :c "
                 "WHERE EXISTS (SELECT 1 FROM catalogo_item "
                 "              WHERE tipo = CAST(:t AS tipo_catalogo) AND codigo = :c) "
                 "ON CONFLICT DO NOTHING"),
            {"id": termo_id, "t": tipo, "c": codigo},
        ).rowcount
    return n


def id_por_norm(sessao: Session) -> dict[str, int]:
    """`termo_norm → id`. Carregado inteiro (499 termos) para resolver `conceitos_origem`."""
    return {n: i for n, i in sessao.execute(text("SELECT termo_norm, id FROM termo")).all()}


def gravar_watermark(sessao: Session, termo_id: int, tipo_doc: str, watermark) -> None:
    """Watermark da coleta incremental por (termo, tipo_doc).

    `GREATEST` no UPDATE: o watermark só anda para a FRENTE. Uma execução que processe um
    lote antigo não pode recuar a marca — recuar significa re-varrer, o que é caro; avançar
    demais significa PULAR documento, o que é perda de dado. O acervo foi semeado de forma
    conservadora justamente para nunca pular (ver CLAUDE.md).
    """
    sessao.execute(
        text("INSERT INTO coleta_watermark (termo_id, tipo_doc, watermark) "
             "VALUES (:id, CAST(:td AS tipo_documento), :w) "
             "ON CONFLICT (termo_id, tipo_doc) DO UPDATE "
             "SET watermark = GREATEST(coleta_watermark.watermark, EXCLUDED.watermark), "
             "    atualizado_em = now()"),
        {"id": termo_id, "td": tipo_doc, "w": watermark},
    )


def contar(sessao: Session) -> tuple[int, int, int]:
    """(termos, ligações termo×código, watermarks)."""
    return (
        sessao.execute(text("SELECT count(*) FROM termo")).scalar_one(),
        sessao.execute(text("SELECT count(*) FROM termo_codigo")).scalar_one(),
        sessao.execute(text("SELECT count(*) FROM coleta_watermark")).scalar_one(),
    )
