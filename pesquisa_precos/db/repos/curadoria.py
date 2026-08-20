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

from pesquisa_precos.db import copia

COLUNAS_RAW = ("tipo", "codigo", "codigo_pdm", "nome_pdm", "descricao",
               "codigo_grupo", "nome_grupo", "nome_classe")


# ── Catálogo completo (etapa 0a) ────────────────────────────────────────────────────

def gravar_raw(conn: psycopg.Connection, linhas: Sequence[Sequence[Any]]) -> int:
    """Upsert em massa do catálogo completo. `linhas` na ordem de `COLUNAS_RAW`.

    `DO UPDATE` pelo mesmo motivo de `catalogo.gravar_itens`: descrição e classe mudam no
    CATMAT, e manter a versão antiga faria o export divergir da fonte oficial em silêncio.
    """
    return copia.copiar(
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
     WHERE (:tipo IS NULL OR p.tipo = CAST(:tipo AS tipo_catalogo))
       AND (:todos OR p.ativo)
     ORDER BY p.tipo, p.ativo DESC, n_itens DESC, p.codigo
"""


def listar_permitidos(sessao: Session, tipo: str | None = None,
                      incluir_inativos: bool = False) -> list[dict]:
    """A allow-list como a tela de curadoria a mostra, já com quantos itens do catálogo
    completo cada código traz. A contagem é o número que torna a decisão informada: um PDM
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
