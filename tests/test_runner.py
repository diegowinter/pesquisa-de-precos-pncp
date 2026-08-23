"""
Guarda da Fase 3 (docs/04_FASES.md): lock com lease, fingerprint/desatualizada, contabilidade
de custo e teto, e o ciclo de vida de `run_step`.

`fingerprint.calcular` é puro e roda sempre. O resto precisa de Postgres com o schema aplicado
(`alembic upgrade head`) e é PULADO sem ele — mesmo padrão de `test_schema_banco.py`. Cada
teste cria seu próprio `run`/`config_version` (não reaproveita nada entre testes) e não deixa
`run_lock` preso: mesmo em caso de falha, o lock é liberado no fim.
"""

import time
from decimal import Decimal

import pytest

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo
from pesquisa_precos.steps.base import TetoDeCustoExcedido
from pesquisa_precos.runner import launcher, fingerprint, lock
from pesquisa_precos.runner.db_context import DbContext

_MOTIVO_SEM_BANCO = f"sem PostgreSQL em {db.database_url()} — rode `alembic upgrade head` antes"
pytestmark_db = pytest.mark.skipif(not db.is_available()[0], reason=_MOTIVO_SEM_BANCO)


# ── fingerprint (puro, sem banco) ────────────────────────────────────────────────────

def test_fingerprint_e_deterministico():
    a = fingerprint.calcular("1.0.0", {"x": 1}, ["dep1", "dep2"])
    b = fingerprint.calcular("1.0.0", {"x": 1}, ["dep1", "dep2"])
    assert a == b


def test_fingerprint_nao_liga_para_ordem_das_dependencias():
    a = fingerprint.calcular("1.0.0", {"x": 1}, ["dep1", "dep2"])
    b = fingerprint.calcular("1.0.0", {"x": 1}, ["dep2", "dep1"])
    assert a == b


@pytest.mark.parametrize("mudanca", [
    lambda: fingerprint.calcular("1.0.1", {"x": 1}, ["dep1"]),   # versao_codigo mudou
    lambda: fingerprint.calcular("1.0.0", {"x": 2}, ["dep1"]),   # params mudaram
    lambda: fingerprint.calcular("1.0.0", {"x": 1}, ["dep1", "dep3"]),  # deps mudaram
])
def test_fingerprint_muda_quando_qualquer_entrada_muda(mudanca):
    base = fingerprint.calcular("1.0.0", {"x": 1}, ["dep1"])
    assert mudanca() != base


# ── fixtures de banco ────────────────────────────────────────────────────────────────

# ── limpeza (a suíte roda contra o banco real) ───────────────────────────────────────

def _limpar_run(run_id: int) -> None:
    """Apaga o run de teste e tudo que pendura nele, na ordem das FKs. `run_lock` aponta para
    `run_step`, então precisa ser solto antes."""
    from sqlalchemy import text

    with db.session() as sessao:
        sessao.execute(text(
            "UPDATE run_lock SET run_etapa_id = NULL WHERE run_etapa_id IN "
            "(SELECT id FROM run_step WHERE run_id = :r)"), {"r": run_id})
        for tabela in ("run_log", "item_error", "llm_call", "run_step"):
            sessao.execute(text(f"DELETE FROM {tabela} WHERE run_id = :r"), {"r": run_id})
        sessao.execute(text("DELETE FROM run WHERE id = :r"), {"r": run_id})


def _limpar_config_versao(cv_id: int) -> None:
    from sqlalchemy import text

    with db.session() as sessao:
        sessao.execute(text("DELETE FROM config_value WHERE config_version_id = :c"),
                       {"c": cv_id})
        sessao.execute(text("DELETE FROM config_version WHERE id = :c"), {"c": cv_id})


@pytest.fixture
def config_version_id():
    if not db.is_available()[0]:
        pytest.skip(_MOTIVO_SEM_BANCO)
    with db.session() as sessao:
        cv_id = repo.criar_config_versao(sessao, "teste-fase3", notes="tests/test_runner.py")
    # commit precisa acontecer ANTES do yield: os testes abrem sessões (conexões) novas, que
    # só enxergam o que já foi commitado — deixar o insert dentro do `with` até o teardown
    # faria toda FK para config_version/run falhar com "key não está presente".
    yield cv_id
    # E o que a fixture cria, a fixture apaga: a suíte roda contra o banco de verdade, e sem
    # isto cada `pytest` deixava uma `config_version`/`run` "teste-fase3" na tela do operador.
    _limpar_config_versao(cv_id)


@pytest.fixture
def run_id(config_version_id):
    with db.session() as sessao:
        r_id = repo.criar_run(sessao, "teste-fase3", config_version_id)
    yield r_id
    _limpar_run(r_id)


@pytest.fixture(autouse=True)
def _lock_limpo():
    """Garante que nenhum teste começa (ou termina) com `run_lock` preso."""
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "UPDATE run_lock SET run_etapa_id=NULL, pid=NULL, "
                "acquired_at=NULL, expires_at=NULL WHERE id=1"))
    yield
    if db.is_available()[0]:
        with db.session() as sessao:
            from sqlalchemy import text
            sessao.execute(text(
                "UPDATE run_lock SET run_etapa_id=NULL, pid=NULL, "
                "acquired_at=NULL, expires_at=NULL WHERE id=1"))


# ── run_step: ciclo de vida ─────────────────────────────────────────────────────────

@pytestmark_db
def test_obter_ou_criar_run_etapa_e_idempotente(run_id):
    with db.session() as sessao:
        a = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        b = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
    assert a == b


@pytestmark_db
def test_params_efetivos_sobrevivem_ao_roundtrip(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        repo.gravar_params(sessao, re_id, effective_params={"tipo": "material", "forcar": True},
                           params_override={"forcar": True})
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
    assert linha["effective_params"] == {"tipo": "material", "forcar": True}
    assert linha["params_override"] == {"forcar": True}


@pytestmark_db
def test_progresso_log_erro_item(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        repo.marcar_executando(sessao, re_id, action="update", pid=1234)
        repo.atualizar_progresso(sessao, re_id, 10, total=100)
        repo.registrar_log(sessao, run_id, "0a", "info", "message de teste", {"k": "v"})
        repo.registrar_erro_item(sessao, run_id, "0a", "key-x", "ValueError", "deu ruim")
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
        logs = repo.logs_do_run(sessao, run_id, step="0a", limite=5)
    assert linha["status"] == "running"
    assert linha["processed"] == 10 and linha["total"] == 100
    assert logs and logs[0]["message"] == "message de teste"
    assert logs[0]["context"] == {"k": "v"}


@pytestmark_db
def test_erro_item_reaproveita_a_linha_em_nova_tentativa(run_id):
    with db.session() as sessao:
        repo.registrar_erro_item(sessao, run_id, "0a", "key-x", "ValueError", "1a tentativa")
        repo.registrar_erro_item(sessao, run_id, "0a", "key-x", "ValueError", "2a tentativa")
        from sqlalchemy import text
        linhas = sessao.execute(
            text("SELECT attempts, message FROM item_error WHERE run_id=:r AND key=:c"),
            {"r": run_id, "c": "key-x"}).all()
    assert len(linhas) == 1
    assert linhas[0].attempts == 2
    assert linhas[0].message == "2a tentativa"


# ── lock com lease ───────────────────────────────────────────────────────────────────

@pytestmark_db
def test_lock_impede_segunda_execucao_concorrente(run_id):
    with db.session() as sessao:
        re1 = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        re2 = repo.obter_ou_criar_run_etapa(sessao, run_id, "1")
        assert lock.tentar_adquirir(sessao, re1, pid=1, timeout_s=60)
        assert not lock.tentar_adquirir(sessao, re2, pid=2, timeout_s=60)
        detentor = lock.quem_detem(sessao)
        assert detentor["run_etapa_id"] == re1
        lock.liberar(sessao, re1)
        assert lock.quem_detem(sessao) is None
        assert lock.tentar_adquirir(sessao, re2, pid=2, timeout_s=60)


@pytestmark_db
def test_lease_expirada_pode_ser_roubada(run_id):
    with db.session() as sessao:
        re1 = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        re2 = repo.obter_ou_criar_run_etapa(sessao, run_id, "1")
        assert lock.tentar_adquirir(sessao, re1, pid=1, timeout_s=1)
    time.sleep(1.2)
    with db.session() as sessao:
        # a lease de re1 expirou — re2 consegue adquirir sem que ninguém tenha liberado nada
        assert lock.tentar_adquirir(sessao, re2, pid=2, timeout_s=60)


@pytestmark_db
def test_leases_expiradas_devolve_run_etapa_travada_a_fila(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "0a")
        repo.marcar_executando(sessao, re_id, action="update", pid=999)
        # simula heartbeat antigo (processo morto sem avisar)
        from sqlalchemy import text
        sessao.execute(text(
            "UPDATE run_step SET heartbeat_at = now() - interval '1 hour' WHERE id=:id"),
            {"id": re_id})
    presas = launcher.recuperar_travados(timeout_s=60)
    assert re_id in presas
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
    assert linha["status"] == "failed"
    # a mensagem é lida pelo operador na tela, não por um dev: sem jargão de lease,
    # e apontando a ação que resolve (o botão "Retomar de onde parou").
    assert "Retomar" in linha["error_message"]
    assert "lease" not in linha["error_message"].lower()


# ── custo e teto (ADR-004) ───────────────────────────────────────────────────────────

@pytestmark_db
def test_gastar_incrementa_run_etapa_e_run(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "3")
        total = repo.incrementar_custo(sessao, re_id, run_id, 1.5)
        total = repo.incrementar_custo(sessao, re_id, run_id, 0.25)
    assert total == Decimal("1.75")
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
    assert linha["cost_usd"] == Decimal("1.75")


@pytestmark_db
def test_contexto_banco_gastar_respeita_teto(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "3")
    with db.session() as db_etapa, db.session() as sessao_execucao:
        ctx = DbContext(
            db_etapa, sessao_execucao=sessao_execucao, run_id=run_id, run_etapa_id=re_id,
            step="3", action="update", mode="assisted", cost_cap_usd=1.0)
        ctx.gastar(0.6)
        with pytest.raises(TetoDeCustoExcedido):
            ctx.gastar(0.6)


@pytestmark_db
def test_contexto_banco_sem_teto_nao_levanta(run_id):
    with db.session() as sessao:
        re_id = repo.obter_ou_criar_run_etapa(sessao, run_id, "3")
    with db.session() as db_etapa, db.session() as sessao_execucao:
        ctx = DbContext(
            db_etapa, sessao_execucao=sessao_execucao, run_id=run_id, run_etapa_id=re_id,
            step="3", action="update", mode="assisted", cost_cap_usd=None)
        ctx.gastar(1000.0)  # não pode levantar — sem teto é sem teto (mesmo espírito do ADR-016)


# ── fingerprint + banco: detecção de "outdated" ─────────────────────────────────

@pytestmark_db
def test_etapa_que_nunca_rodou_nao_e_desatualizada(run_id, monkeypatch):
    # `ultimo_fingerprint_concluido` é global por step (não por run) por desenho — em uma base
    # de teste reaproveitada entre execuções, "0a" pode já ter rodado em um teste anterior.
    # Isola o caso "nunca rodou" sem depender do histórico real da tabela (ADR-007: nunca se
    # apaga `run_step`, então "resetar" o estado global não é uma opção aqui).
    monkeypatch.setattr(repo, "ultimo_fingerprint_concluido", lambda *a, **k: None)
    with db.session() as sessao:
        assert fingerprint.esta_desatualizada(sessao, "0a", {"tipo": "ambos"}) is False


@pytestmark_db
def test_etapa_fica_desatualizada_quando_params_mudam(run_id, monkeypatch):
    gravado: dict[str, str | None] = {"fp": None}

    def fake_ultimo_fingerprint(sessao_, etapa_):
        return gravado["fp"] if etapa_ == "0a" else None

    monkeypatch.setattr(repo, "ultimo_fingerprint_concluido", fake_ultimo_fingerprint)
    with db.session() as sessao:
        gravado["fp"] = fingerprint.calcular_para_etapa(sessao, "0a", {"tipo": "material"})
        assert fingerprint.esta_desatualizada(sessao, "0a", {"tipo": "material"}) is False
        assert fingerprint.esta_desatualizada(sessao, "0a", {"tipo": "servico"}) is True


# ── launcher.preparar: resolução de params em camadas (docs/03_ETAPAS.md §3) ─────────

@pytestmark_db
def test_preparar_aplica_camada_de_config_sobre_o_default(run_id):
    with db.session() as sessao:
        repo.gravar_config(sessao, repo.run_por_id(sessao, run_id)["config_version_id"],
                           {"so_grupos_seguranca": False})
    re_id = launcher.preparar(run_id, "0a")
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
    assert linha["effective_params"]["so_grupos_seguranca"] is False


@pytestmark_db
def test_preparar_override_vence_config(run_id):
    with db.session() as sessao:
        repo.gravar_config(sessao, repo.run_por_id(sessao, run_id)["config_version_id"],
                           {"so_grupos_seguranca": False})
    re_id = launcher.preparar(run_id, "0a", params_override={"so_grupos_seguranca": True})
    with db.session() as sessao:
        linha = repo.run_etapa_por_id(sessao, re_id)
    assert linha["effective_params"]["so_grupos_seguranca"] is True
    assert linha["params_override"] == {"so_grupos_seguranca": True}
