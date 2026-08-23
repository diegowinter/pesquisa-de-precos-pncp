"""
Repositório de termos de busca (`termo`, `termo_codigo`, `collection_watermark`).

`termo_norm` é a chave de dedup (UNIQUE) e vem de `core.text.normalizar_termo`, que
**preserva o acento** — ao contrário de `normalizar_texto`, usada no `texto_hash`. Não é
inconsistência: "ambulancia" e "ambulância" são duas buscas diferentes no PNCP e a etapa 1 as
gera de propósito. A justificativa completa está na docstring de `normalizar_termo`.
"""

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.core.text import normalizar_termo


def upsert(sessao: Session, termo_txt: str, categoria: str | None,
           source: str | None) -> int | None:
    """Insere (ou reaproveita) o termo e devolve seu id. `None` se o texto for vazio.

    `ON CONFLICT DO UPDATE` com um `SET` inócuo em vez de `DO NOTHING`: só o UPDATE faz o
    Postgres devolver a linha no `RETURNING` quando ela já existia. Com `DO NOTHING`, o
    `RETURNING` vem vazio e seria preciso um SELECT extra por termo (499 round-trips).
    """
    norm = normalizar_termo(termo_txt)
    if not norm:
        return None
    return sessao.execute(
        text("INSERT INTO termo (termo, termo_norm, categoria, source) "
             "VALUES (:t, :n, :c, :o) "
             "ON CONFLICT (termo_norm) DO UPDATE SET termo = termo.termo "
             "RETURNING id"),
        {"t": termo_txt, "n": norm, "c": categoria or None, "o": source or None},
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


def watermarks(sessao: Session) -> dict[tuple[int, str], str]:
    """`(termo_id, tipo_doc) → watermark ISO` — o `carregar_watermark()` da etapa 2 em SQL.

    Devolve string ISO, não `datetime`: a etapa compara com `data_atualizacao_pncp` como vem
    da API, que é texto. Converter dos dois lados só criaria uma chance a mais de erro de
    fuso numa comparação que hoje é lexicográfica e funciona.
    """
    return {
        (tid, td): wm.isoformat() if hasattr(wm, "isoformat") else str(wm)
        for tid, td, wm in sessao.execute(text(
            "SELECT termo_id, tipo_doc::text, watermark FROM collection_watermark")).all()
    }


def gravar_watermark(sessao: Session, termo_id: int, tipo_doc: str, watermark) -> None:
    """Watermark da coleta incremental por (termo, tipo_doc).

    `GREATEST` no UPDATE: o watermark só anda para a FRENTE. Uma execução que processe um
    lote antigo não pode recuar a marca — recuar significa re-varrer, o que é caro; avançar
    demais significa PULAR documento, o que é perda de dado. O acervo foi semeado de forma
    conservadora justamente para nunca pular (ver CLAUDE.md).
    """
    sessao.execute(
        text("INSERT INTO collection_watermark (termo_id, tipo_doc, watermark) "
             "VALUES (:id, CAST(:td AS tipo_documento), :w) "
             "ON CONFLICT (termo_id, tipo_doc) DO UPDATE "
             "SET watermark = GREATEST(collection_watermark.watermark, EXCLUDED.watermark), "
             "    updated_at = now()"),
        {"id": termo_id, "td": tipo_doc, "w": watermark},
    )


# ── Etapa 1 no banco (Fase 10) ──────────────────────────────────────────────────────

def geracoes(sessao: Session) -> dict[tuple[str, str], dict]:
    """`(tipo, codigo) → {'termos': [...], 'categoria': str}` — o `_ler_checkpoint()` da
    etapa 1, em SQL. Formato idêntico ao do CSV para que a agregação e a cascata de categoria
    rodem sem saber de onde vieram."""
    return {
        (tipo, codigo): {"termos": list(termos or []), "categoria": categoria or ""}
        for tipo, codigo, termos, categoria in sessao.execute(text(
            "SELECT tipo::text, codigo, termos, categoria_llm FROM termo_geracao")).all()
    }


def gravar_geracao(sessao: Session, tipo: str, codigo: str, termos: Sequence[str],
                   categoria_llm: str | None, *, model: str | None = None,
                   provider: str | None = None, run_id: int | None = None) -> None:
    """Cache da chamada de LLM de UM item do catálogo. É a marca de resumo da etapa 1:
    item presente aqui não volta ao modelo."""
    sessao.execute(text("""
        INSERT INTO termo_geracao (tipo, codigo, termos, categoria_llm, model, provider, run_id)
        VALUES (CAST(:t AS tipo_catalogo), :c, :termos, :cat, :model, :prov, :run)
        ON CONFLICT (tipo, codigo) DO UPDATE
           SET termos = EXCLUDED.termos, categoria_llm = EXCLUDED.categoria_llm,
               model = EXCLUDED.model, provider = EXCLUDED.provider,
               run_id = EXCLUDED.run_id, created_at = now()
    """), {"t": tipo, "c": codigo, "termos": list(termos), "cat": categoria_llm or None,
           "model": model, "prov": provider, "run": run_id})


def codigos_ja_gerados(sessao: Session) -> set[tuple[str, str]]:
    """Chave de resumo da etapa 1 — o que `ler_chaves_concluidas()` fazia sobre o CSV."""
    return {(t, c) for t, c in sessao.execute(
        text("SELECT tipo::text, codigo FROM termo_geracao")).all()}


def gravar_categorias(sessao: Session, categorias: dict[str, str]) -> int:
    """Categoria final (pós-cascata) em `catalogo_item` — o que era
    `1_categoria_por_codigo.csv`, a fonte canônica por item do pareamento da 6a."""
    n = 0
    for codigo, categoria in categorias.items():
        n += sessao.execute(
            text("UPDATE catalogo_item SET categoria = :cat, updated_at = now() "
                 "WHERE codigo = :cod AND categoria IS DISTINCT FROM :cat"),
            {"cat": categoria, "cod": codigo}).rowcount
    return n


def desativar_llm_ausentes(sessao: Session, termos_norm: Sequence[str]) -> int:
    """Reconstrói o conjunto `source='llm'`: o que não foi gerado desta vez sai de cena.

    Espelha o `mesclar_preservando_manual()` do caminho CSV, que reescreve as linhas de LLM e
    preserva as manuais. Aqui DESATIVA em vez de apagar — `termo_codigo` e `collection_watermark`
    pendem do id, e apagar o termo levaria junto o watermark da coleta (re-varredura completa
    do PNCP na próxima atualização, que é caro e silencioso).
    """
    return sessao.execute(text("""
        UPDATE termo SET active = false, excluido_por = 'etapa1', excluido_em = now()
         WHERE active AND coalesce(source, 'llm') <> 'manual'
           AND NOT (termo_norm = ANY(:norms))
    """), {"norms": list(termos_norm)}).rowcount


def contar(sessao: Session) -> tuple[int, int, int]:
    """(termos, ligações termo×código, watermarks)."""
    return (
        sessao.execute(text("SELECT count(*) FROM termo")).scalar_one(),
        sessao.execute(text("SELECT count(*) FROM termo_codigo")).scalar_one(),
        sessao.execute(text("SELECT count(*) FROM collection_watermark")).scalar_one(),
    )
