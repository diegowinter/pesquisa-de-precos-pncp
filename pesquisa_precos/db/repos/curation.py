"""
Repositório da curadoria de catálogo (`catalogo_raw`, `pdm_permitido`) — Fase 10, ADR-017.

O que este módulo substitui: as constantes `PDMS_MATERIAIS`/`CODIGOS_SERVICOS` de
`core/catalogo/local.py`. O MÉTODO (como filtrar) continua lá; os DADOS (o que filtrar) passam
a vir daqui, editáveis pela interface sem deploy.

A peça central é `derivar_catalogo_item()`: `catalogo_item` deixa de ser carregado de um CSV já
filtrado e passa a ser **derivado** de `catalogo_raw ∩ pdm_permitido` por SQL. Isso é o que
torna a recuradoria barata — mudar a allow-list na tela e reprojetar o catálogo é um comando,
não uma reexecução da etapa 0a (que rebaixaria o catálogo inteiro da API).

Assimetria que atravessa o módulo inteiro: o `codigo` de `pdm_permitido` casa com
`catalogo_raw.codigo_pdm` para material e com `catalogo_raw.codigo` para serviço. É herança da
API de Dados Abertos (materiais são agrupados por PDM, serviços não têm PDM), não escolha
nossa — e é por isso que a derivação tem dois ramos em vez de um join só.
"""

from collections.abc import Sequence
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from pesquisa_precos.db import copy

COLUNAS_RAW = ("tipo", "codigo", "codigo_pdm", "nome_pdm", "descricao",
               "codigo_grupo", "nome_grupo", "nome_classe")


# ── Catálogo completo (etapa 0a) ────────────────────────────────────────────────────

def gravar_raw(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert em massa do catálogo completo. `linhas` na ordem de `COLUNAS_RAW`.

    `DO UPDATE` pelo mesmo motivo de `catalogo.gravar_itens`: descrição e classe mudam no
    CATMAT, e manter a versão antiga faria o export divergir da fonte oficial em silêncio.
    """
    return copy.copiar(
        conn, "catalogo_raw", COLUNAS_RAW, linhas,
        conflito=("tipo", "codigo"),
        atualizar=("codigo_pdm", "nome_pdm", "descricao",
                   "codigo_grupo", "nome_grupo", "nome_classe"),
    )


def contar_raw(sessao: Session, tipo: str | None = None) -> int:
    if tipo:
        return sessao.execute(
            text("SELECT count(*) FROM catalogo_raw WHERE tipo = CAST(:t AS tipo_catalogo)"),
            {"t": tipo}).scalar_one()
    return sessao.execute(text("SELECT count(*) FROM catalogo_raw")).scalar_one()


# ── Allow-list (o que a interface edita) ────────────────────────────────────────────

SQL_LISTAR = """
    SELECT p.tipo::text, p.codigo, p.nome, p.observacao, p.ativo,
           p.criado_por, p.criado_em,
           (SELECT count(*) FROM catalogo_raw r
             WHERE r.tipo = p.tipo
               AND (CASE WHEN p.tipo = 'material' THEN r.codigo_pdm ELSE r.codigo END)
                   = p.codigo) AS n_itens
      FROM pdm_permitido p
     WHERE (CAST(:tipo AS text) IS NULL OR p.tipo::text = CAST(:tipo AS text))
       AND (CAST(:todos AS boolean) OR p.ativo)
     ORDER BY p.tipo, p.ativo DESC, n_itens DESC, p.codigo
"""


def listar_permitidos(sessao: Session, tipo: str | None = None,
                      incluir_inativos: bool = False) -> list[dict]:
    """A allow-list como a tela de curadoria a mostra, já com quantos itens do catálogo
    completo cada código traz.

    Os `CAST(:param AS tipo)` no WHERE não são decoração: com `tipo=None` (listar os dois
    tipos), o Postgres não consegue inferir o tipo do parâmetro e levanta `AmbiguousParameter`.

    A contagem por código A contagem é o número que torna a decisão informada: um PDM
    que casa 0 itens é curadoria morta, e hoje não há como perceber isso sem rodar a 0a."""
    linhas = sessao.execute(text(SQL_LISTAR), {"tipo": tipo, "todos": incluir_inativos}).all()
    return [
        {"tipo": t, "codigo": c, "nome": n, "observacao": o, "ativo": a,
         "criado_por": por, "criado_em": em, "n_itens": qtd}
        for t, c, n, o, a, por, em, qtd in linhas
    ]


SQL_PERMITIR = """
    INSERT INTO pdm_permitido (tipo, codigo, nome, observacao, ativo, criado_por)
    VALUES (CAST(:tipo AS tipo_catalogo), :codigo, :nome, :obs, true, :por)
    ON CONFLICT (tipo, codigo) DO UPDATE
       SET ativo = true,
           nome = COALESCE(EXCLUDED.nome, pdm_permitido.nome),
           observacao = COALESCE(EXCLUDED.observacao, pdm_permitido.observacao),
           atualizado_em = now()
"""


def permitir(sessao: Session, tipo: str, codigo: str, *, nome: str | None = None,
             observacao: str | None = None, criado_por: str | None = None) -> None:
    """Inclui (ou reativa) um código na allow-list. Idempotente."""
    sessao.execute(text(SQL_PERMITIR), {"tipo": tipo, "codigo": str(codigo), "nome": nome,
                                        "obs": observacao, "por": criado_por})


SQL_REVOGAR = """
    UPDATE pdm_permitido
       SET ativo = false,
           observacao = COALESCE(:motivo, observacao),
           atualizado_em = now()
     WHERE tipo = CAST(:tipo AS tipo_catalogo) AND codigo = :codigo AND ativo
"""


def revogar(sessao: Session, tipo: str, codigo: str, *, motivo: str | None = None) -> int:
    """Tira um código do escopo. DESATIVA, nunca apaga — mesmo princípio de
    `catalogo.marcar_inativos`: o código já foi origem de linhas de export entregues, e o
    motivo da exclusão é justamente o que se perde primeiro."""
    return sessao.execute(
        text(SQL_REVOGAR),
        {"tipo": tipo, "codigo": str(codigo), "motivo": motivo}).rowcount


def codigos_ativos(sessao: Session, tipo: str) -> set[str]:
    """A allow-list crua, para quem ainda filtra em Python (o caminho `--fonte csv`)."""
    return set(sessao.scalars(
        text("SELECT codigo FROM pdm_permitido "
             "WHERE tipo = CAST(:t AS tipo_catalogo) AND ativo"),
        {"t": tipo}).all())


# ── Grupos de segurança (recorte do download, não do escopo) ────────────────────────
#
# Cuidado ao ler junto com `pdm_permitido`: as duas são curadoria, mas de coisas diferentes.
# `grupo_permitido` só é consultada quando a 0a roda com `--so-grupos-seguranca`, e serve para
# baixar menos catálogo. Quem decide o que entra na pesquisa continua sendo `pdm_permitido`.

def grupos_ativos(sessao: Session, tipo: str) -> list[str]:
    """`codigoGrupo` a paginar no download recortado. Ordenado para o log ficar estável."""
    return list(sessao.scalars(
        text("SELECT codigo FROM grupo_permitido "
             "WHERE tipo = CAST(:t AS tipo_catalogo) AND ativo "
             "ORDER BY length(codigo), codigo"),
        {"t": tipo}).all())


SQL_LISTAR_GRUPOS = """
    SELECT g.tipo::text, g.codigo, g.nome, g.observacao, g.ativo, g.criado_por, g.criado_em,
           (SELECT count(*) FROM catalogo_raw r
             WHERE r.tipo = g.tipo AND r.codigo_grupo = g.codigo) AS n_itens
      FROM grupo_permitido g
     WHERE (CAST(:tipo AS text) IS NULL OR g.tipo::text = CAST(:tipo AS text))
       AND (CAST(:todos AS boolean) OR g.ativo)
     ORDER BY g.tipo, g.ativo DESC, length(g.codigo), g.codigo
"""


def listar_grupos(sessao: Session, tipo: str | None = None,
                  incluir_inativos: bool = False) -> list[dict]:
    """Os grupos como a tela de curadoria os mostra, com quantos itens do catálogo completo
    cada um traz. Os `CAST` existem pelo mesmo motivo de `listar_permitidos`: parâmetro NULL
    sem tipo faz o Postgres levantar `AmbiguousParameter`."""
    linhas = sessao.execute(text(SQL_LISTAR_GRUPOS),
                            {"tipo": tipo, "todos": incluir_inativos}).all()
    return [
        {"tipo": t, "codigo": c, "nome": n, "observacao": o, "ativo": a,
         "criado_por": por, "criado_em": em, "n_itens": qtd}
        for t, c, n, o, a, por, em, qtd in linhas
    ]


def permitir_grupo(sessao: Session, tipo: str, codigo: str, *, nome: str | None = None,
                   observacao: str | None = None, criado_por: str | None = None) -> None:
    sessao.execute(text("""
        INSERT INTO grupo_permitido (tipo, codigo, nome, observacao, ativo, criado_por)
        VALUES (CAST(:tipo AS tipo_catalogo), :codigo, :nome, :obs, true, :por)
        ON CONFLICT (tipo, codigo) DO UPDATE
           SET ativo = true,
               nome = COALESCE(EXCLUDED.nome, grupo_permitido.nome),
               observacao = COALESCE(EXCLUDED.observacao, grupo_permitido.observacao),
               atualizado_em = now()
    """), {"tipo": tipo, "codigo": str(codigo), "nome": nome,
           "obs": observacao, "por": criado_por})


def revogar_grupo(sessao: Session, tipo: str, codigo: str, *,
                  motivo: str | None = None) -> int:
    """Desativa, nunca apaga — mesmo princípio do resto da curadoria.

    Revogar um grupo NÃO tira do escopo os itens já baixados: `catalogo_item` continua vindo
    de `pdm_permitido`. O efeito é só no próximo download recortado.
    """
    return sessao.execute(text("""
        UPDATE grupo_permitido
           SET ativo = false, observacao = COALESCE(:motivo, observacao), atualizado_em = now()
         WHERE tipo = CAST(:tipo AS tipo_catalogo) AND codigo = :codigo AND ativo
    """), {"tipo": tipo, "codigo": str(codigo), "motivo": motivo}).rowcount


# ── Derivação: catalogo_raw ∩ pdm_permitido → catalogo_item ─────────────────────────

DERIVACAO = """
INSERT INTO catalogo_item (tipo, codigo, codigo_pdm, nome_pdm, descricao,
                           codigo_grupo, nome_grupo, nome_classe, ativo)
SELECT r.tipo, r.codigo, r.codigo_pdm, r.nome_pdm, r.descricao,
       r.codigo_grupo, r.nome_grupo, r.nome_classe, true
  FROM catalogo_raw r
  JOIN pdm_permitido p
    ON p.tipo = r.tipo
   AND p.ativo
   AND p.codigo = CASE WHEN r.tipo = 'material' THEN r.codigo_pdm ELSE r.codigo END
ON CONFLICT (tipo, codigo) DO UPDATE
   SET codigo_pdm = EXCLUDED.codigo_pdm,
       nome_pdm = EXCLUDED.nome_pdm,
       descricao = EXCLUDED.descricao,
       codigo_grupo = EXCLUDED.codigo_grupo,
       nome_grupo = EXCLUDED.nome_grupo,
       nome_classe = EXCLUDED.nome_classe,
       ativo = true,
       atualizado_em = now()
"""

# Item que saiu do escopo (o PDM foi revogado) some do `catalogo_item`? NÃO: vira inativo.
# `catalogo_item.categoria` vem da etapa 1 e é cara (LLM); apagar a linha jogaria fora esse
# trabalho, e o código continua sendo a origem de linhas de export já entregues.
DESATIVACAO = """
UPDATE catalogo_item c
   SET ativo = false, atualizado_em = now()
 WHERE c.ativo
   AND NOT EXISTS (
        SELECT 1 FROM catalogo_raw r
          JOIN pdm_permitido p
            ON p.tipo = r.tipo
           AND p.ativo
           AND p.codigo = CASE WHEN r.tipo = 'material' THEN r.codigo_pdm ELSE r.codigo END
         WHERE r.tipo = c.tipo AND r.codigo = c.codigo)
"""


def derivar_catalogo_item(sessao: Session) -> dict[str, int]:
    """Recomputa `catalogo_item` a partir do catálogo completo e da allow-list ativa.

    Chamada pela etapa 0a e por qualquer edição de curadoria na interface — é o que faz
    "mudei a allow-list" ter efeito sem reexecutar a etapa (que rebaixaria a API inteira).

    NÃO toca em `categoria`: ela vem da etapa 1, custa LLM e não é derivável daqui. O
    `ON CONFLICT DO UPDATE` lista as colunas uma a uma exatamente por isso — um
    `SET (...) = (EXCLUDED...)` genérico apagaria a categoria de todo código a cada
    rederivação.
    """
    inseridos = sessao.execute(text(DERIVACAO)).rowcount
    desativados = sessao.execute(text(DESATIVACAO)).rowcount
    total = sessao.execute(
        text("SELECT count(*) FROM catalogo_item WHERE ativo")).scalar_one()
    return {"derivados": inseridos, "desativados": desativados, "ativos": total}


# ── Checkpoint de download (o que era checkpoints/0a_parts_<tipo>/) ─────────────────

def paginas_baixadas(sessao: Session, tipo: str, prefixo: str) -> set[int]:
    """Páginas já gravadas — o `if parte.exists()` do caminho em disco, em SQL."""
    return set(sessao.scalars(
        text("SELECT pagina FROM catalogo_download "
             "WHERE tipo = CAST(:t AS tipo_catalogo) AND prefixo = :p"),
        {"t": tipo, "p": prefixo}).all())


def marcar_pagina(sessao: Session, tipo: str, prefixo: str, pagina: int,
                  n_linhas: int) -> None:
    """Registra a página DEPOIS de gravar suas linhas em `catalogo_raw`.

    A ordem importa e é a mesma do caminho em disco: marcar antes faria uma queda no meio
    pular uma página que nunca foi gravada. Como ambas as escritas caem na mesma transação,
    ou as duas valem ou nenhuma vale.
    """
    sessao.execute(text("""
        INSERT INTO catalogo_download (tipo, prefixo, pagina, n_linhas)
        VALUES (CAST(:t AS tipo_catalogo), :p, :pag, :n)
        ON CONFLICT (tipo, prefixo, pagina) DO UPDATE
           SET n_linhas = EXCLUDED.n_linhas, baixado_em = now()
    """), {"t": tipo, "p": prefixo, "pag": pagina, "n": n_linhas})


def limpar_download(sessao: Session, tipo: str) -> int:
    """`--forcar`: descarta o checkpoint para rebaixar do zero. `catalogo_raw` NÃO é apagado —
    o upsert reescreve o que voltar, e apagar deixaria o catálogo vazio no meio do download."""
    return sessao.execute(
        text("DELETE FROM catalogo_download WHERE tipo = CAST(:t AS tipo_catalogo)"),
        {"t": tipo}).rowcount


# ── Snapshot e delta (o que eram 0a_catalogo_snapshot.csv / 0a_catalogo_delta.csv) ──

SQL_SNAPSHOT_ANTERIOR = """
    SELECT tipo::text, codigo FROM catalogo_snapshot
     WHERE capturado_em = (SELECT max(capturado_em) FROM catalogo_snapshot)
"""

SQL_GRAVAR_SNAPSHOT = """
    INSERT INTO catalogo_snapshot (capturado_em, tipo, codigo, hash_linha)
    SELECT :agora, tipo, codigo, md5(coalesce(descricao, '') || coalesce(nome_pdm, ''))
      FROM catalogo_item WHERE ativo
"""


def delta_catalogo(sessao: Session) -> dict[str, int]:
    """Compara `catalogo_item` ativo com o último snapshot e captura um novo. Substitui
    `gerar_delta_catalogo()` — mesma semântica, sem CSV.

    Primeira execução (sem snapshot anterior): estabelece a linha de base e devolve delta
    ZERO. Isso é deliberado e igual ao caminho em disco — marcar um catálogo inteiro já
    coletado como "novo" é a armadilha que o comentário original da etapa já registrava.
    """
    from datetime import UTC, datetime

    anterior = {(t, c) for t, c in sessao.execute(text(SQL_SNAPSHOT_ANTERIOR)).all()}
    atual = {(t, c) for t, c in sessao.execute(text(
        "SELECT tipo::text, codigo FROM catalogo_item WHERE ativo")).all()}

    primeira = not anterior
    novos = set() if primeira else atual - anterior
    removidos = set() if primeira else anterior - atual

    sessao.execute(text(SQL_GRAVAR_SNAPSHOT), {"agora": datetime.now(UTC)})
    return {"codigos_novos": len(novos), "codigos_removidos": len(removidos),
            "baseline": int(primeira)}
