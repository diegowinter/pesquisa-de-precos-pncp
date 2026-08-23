"""
Guarda anti-drift entre os modelos SQLAlchemy e o schema real do PostgreSQL.

O schema é criado pela migration `0001_schema_inicial` (DDL literal de docs/02_SCHEMA.md, que é
normativo); os modelos de `db/modelos.py` são um espelho manual dele. Espelho manual entorta:
alguém acrescenta uma coluna na migration e esquece o modelo, e o sintoma é um `SELECT` do
repositório que devolve dados incompletos — sem erro.

Este teste compara os dois por reflexão. Ele é PULADO quando não há banco disponível, para que
`pytest` continue rodando em segundos numa máquina sem Postgres — o que também significa que
ele só protege de verdade quando rodado com o banco de pé. Rode-o depois de qualquer migration.
"""

import pytest
from sqlalchemy import inspect

from pesquisa_precos.db import session as db
from pesquisa_precos.db.enums import NOMES
from pesquisa_precos.db.models import Base

pytestmark = pytest.mark.skipif(
    not db.is_available()[0],
    reason=f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes")


@pytest.fixture(scope="module")
def inspetor():
    return inspect(db.engine())


def test_todas_as_tabelas_dos_modelos_existem(inspetor):
    no_banco = set(inspetor.get_table_names())
    nos_modelos = set(Base.metadata.tables)
    faltando = nos_modelos - no_banco
    assert not faltando, f"tabelas no modelo e não no banco: {sorted(faltando)}"


def test_nenhuma_tabela_do_banco_ficou_sem_modelo(inspetor):
    # `alembic_version` é do próprio Alembic e não tem (nem deve ter) modelo.
    no_banco = set(inspetor.get_table_names()) - {"alembic_version"}
    sem_modelo = no_banco - set(Base.metadata.tables)
    assert not sem_modelo, f"tabelas no banco e não no modelo: {sorted(sem_modelo)}"


def test_colunas_batem(inspetor):
    divergencias = []
    for name, tabela in Base.metadata.tables.items():
        if name not in inspetor.get_table_names():
            continue
        reais = {c["name"] for c in inspetor.get_columns(name)}
        modelo = set(tabela.columns.keys())
        if faltam := modelo - reais:
            divergencias.append(f"{name}: no modelo e não no banco {sorted(faltam)}")
        if sobram := reais - modelo:
            divergencias.append(f"{name}: no banco e não no modelo {sorted(sobram)}")
    assert not divergencias, "\n".join(divergencias)


def test_valores_dos_enums_batem(inspetor):
    """Um valor de enum que só existe no Python vira erro de INSERT em produção, não aqui."""
    do_banco = {e["name"]: set(e["labels"]) for e in inspetor.get_enums()}
    for name, classe in NOMES.items():
        assert name in do_banco, f"tipo enum {name} não existe no banco"
        do_python = {v.value for v in classe}
        assert do_python == do_banco[name], (
            f"{name}: python={sorted(do_python)} banco={sorted(do_banco[name])}")


def test_indices_que_sao_comportamento_existem(inspetor):
    """Índices parciais e GIN não são estética: sem eles a consulta muda de plano e a etapa
    fica lenta o bastante para parecer travada. O autogenerate do Alembic não os reproduz, o
    que é exatamente por que a migration inicial é SQL literal."""
    esperados = {
        "catalogo_item": "ix_catalogo_categoria",
        "item": "ix_item_sobrevivente",
        "par": "ix_par_codigo",
        "texto_classificacao": "ix_texto_classif_cats",
        "erro_item": "ix_erro_pendente",
    }
    for tabela, indice in esperados.items():
        nomes = {i["name"] for i in inspetor.get_indexes(tabela)}
        assert indice in nomes, f"{tabela}: índice {indice} ausente (tem {sorted(nomes)})"


def test_coluna_gerada_de_documento_pagina(inspetor):
    """`n_chars` é GENERATED ALWAYS: se virar coluna comum, o `COPY` da migração passa a
    exigir o valor e a política de retenção perde o número que usa para medir o ganho."""
    colunas = {c["name"]: c for c in inspetor.get_columns("documento_pagina")}
    assert colunas["n_chars"].get("computed"), "n_chars deixou de ser coluna gerada"
