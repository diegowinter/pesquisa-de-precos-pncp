"""
Engine, sessão e conexão bruta com o PostgreSQL.

Ponto único de acesso ao banco — nenhum outro módulo monta URL de conexão à mão, pelo mesmo
motivo que nenhum escreve caminho de `data/` literal: divergência silenciosa. Um script de
migração que aponte para outro banco não levanta exceção; ele só popula o banco errado.

A URL vem de `DATABASE_URL` no `.env`. O default aponta para o Postgres local do usuário
(porta 5432, base `pesquisa_precos`), porque é o desenho do ADR-001: um processo, um banco,
uma máquina. Não há credencial no código — só o default de desenvolvimento, sobrescrevível.

`raw_connection()` devolve a conexão psycopg pura porque a migração usa `COPY`, que é a única
forma de ingerir 1,6 milhão de linhas em tempo razoável e não tem equivalente no ORM.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pesquisa_precos.config import settings  # noqa: F401  (efeito colateral: carrega o .env)

DEFAULT_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/pesquisa_precos"

_engine: Engine | None = None
_Sessao: sessionmaker[Session] | None = None


def database_url() -> str:
    return os.getenv("DATABASE_URL") or DEFAULT_URL


def engine() -> Engine:
    """Engine singleton. `pool_pre_ping` porque o processo de etapa vive horas e o Postgres
    local pode ser reiniciado no meio — sem isso a conexão morta só aparece como erro no
    primeiro INSERT depois da queda, muito longe da causa."""
    global _engine, _Sessao
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
        _Sessao = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def create_session() -> Session:
    engine()
    assert _Sessao is not None
    return _Sessao()


@contextmanager
def session() -> Iterator[Session]:
    """Sessão transacional: commit no fim, rollback em qualquer exceção.

    Resultado de uma unidade de trabalho e avanço do seu estado devem ficar DENTRO do mesmo
    `with` (docs/08_CONVENCOES.md §5.3). Separar os dois faz a retomada pagar o LLM de novo.
    """
    s = create_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def raw_connection():
    """Conexão psycopg crua (para `COPY`). Commit/rollback pelo mesmo contrato do `sessao()`."""
    raw = engine().raw_connection()
    try:
        yield raw.driver_connection
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def is_available() -> tuple[bool, str]:
    """(ok, mensagem) — usado pelo `--fonte banco` para falhar com mensagem útil em vez de
    stack trace de driver quando o serviço está fora ou a base não existe."""
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True, database_url()
    except Exception as exc:  # noqa: BLE001 — a mensagem do driver é o diagnóstico
        return False, f"{type(exc).__name__}: {exc}"
