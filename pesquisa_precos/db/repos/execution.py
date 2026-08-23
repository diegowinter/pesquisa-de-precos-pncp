"""
Repositório de execução e configuração (`run`, `run_step`, `config_version`, `config_value`,
`prompt`, `prompt_version`, `provider`).

Na Fase 2 estas tabelas existem, mas o ciclo de vida completo (lock, heartbeat, custo, teto,
fingerprint) é entrega da Fase 3. O que está aqui é o mínimo para que os dados migrados tenham
um `run_id` válido a que se prender — sem isso, `grupo_item.run_id NOT NULL` deixaria o acervo
histórico impossível de migrar.

Daí o "run sintético" do m03: um `run` marcado `concluido`, rotulado como o acervo herdado.
Ele não é uma execução de verdade e não deve ser tratado como uma.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

ROTULO_ACERVO_MIGRADO = "acervo migrado v2/v3"


def criar_config_versao(sessao: Session, label: str, created_by: str | None = None,
                        notes: str | None = None) -> int:
    """Config é IMUTÁVEL: editar cria versão nova (ADR-014). Isto só cria."""
    return sessao.execute(
        text("INSERT INTO config_version (label, created_by, notes) "
             "VALUES (:r, :p, :n) RETURNING id"),
        {"r": label, "p": created_by, "n": notes},
    ).scalar_one()


def config_versao_por_rotulo(sessao: Session, label: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM config_version WHERE label = :r ORDER BY id DESC LIMIT 1"),
        {"r": label}).scalar()


def gravar_config(sessao: Session, config_version_id: int, valores: dict[str, Any]) -> int:
    """Valores de uma versão de config. `value` é `jsonb` — número é número, não string."""
    import json
    n = 0
    for key, value in valores.items():
        n += sessao.execute(
            text("INSERT INTO config_value (config_version_id, key, value) "
                 "VALUES (:v, :c, CAST(:j AS jsonb)) "
                 "ON CONFLICT (config_version_id, key) DO UPDATE SET value = EXCLUDED.value"),
            {"v": config_version_id, "c": key, "j": json.dumps(value)},
        ).rowcount
    return n


def ler_config(sessao: Session, config_version_id: int) -> dict[str, Any]:
    return {c: v for c, v in sessao.execute(
        text("SELECT key, value FROM config_value WHERE config_version_id = :v"),
        {"v": config_version_id}).all()}


def listar_config_versoes(sessao: Session) -> list[dict[str, Any]]:
    """Todas as versões, mais recente primeiro — tela de configuração (docs/06_API_E_WEB.md
    §4.5). Config é IMUTÁVEL (ADR-014): esta lista é o histórico completo, nunca sobrescrito."""
    linhas = sessao.execute(
        text("SELECT id, label, created_by, created_at, notes FROM config_version "
             "ORDER BY id DESC")).mappings().all()
    return [dict(linha) for linha in linhas]


def config_versao_por_id(sessao: Session, config_version_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, label, created_by, created_at, notes FROM config_version "
             "WHERE id = :id"), {"id": config_version_id}).mappings().first()
    if linha is None:
        return None
    dado = dict(linha)
    dado["valores"] = ler_config(sessao, config_version_id)
    return dado


def diff_config(sessao: Session, id_a: int, id_b: int) -> dict[str, Any]:
    """Diferença chave a chave entre duas `config_version` (docs/06_API_E_WEB.md
    `GET /api/config/versions/{id}/diff/{outro_id}`). Só reporta chaves que mudaram — chave
    ausente de um lado aparece com valor `None` do lado que não a define."""
    valores_a = ler_config(sessao, id_a)
    valores_b = ler_config(sessao, id_b)
    chaves = sorted(set(valores_a) | set(valores_b))
    diferencas = [
        {"key": c, "de": valores_a.get(c), "para": valores_b.get(c)}
        for c in chaves if valores_a.get(c) != valores_b.get(c)
    ]
    return {"config_versao_a": id_a, "config_versao_b": id_b, "diferencas": diferencas}


def criar_run(sessao: Session, label: str, config_version_id: int, *,
              mode: str = "assisted", status: str = "open",
              created_by: str | None = None) -> int:
    return sessao.execute(
        text("INSERT INTO run (label, mode, status, config_version_id, created_by, "
             "                 finished_at) "
             "VALUES (:r, CAST(:m AS run_mode), CAST(:s AS run_status), :cv, :p, "
             "        CASE WHEN :s = 'finished' THEN now() ELSE NULL END) "
             "RETURNING id"),
        {"r": label, "m": mode, "s": status, "cv": config_version_id, "p": created_by},
    ).scalar_one()


def run_por_rotulo(sessao: Session, label: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM run WHERE label = :r ORDER BY id DESC LIMIT 1"),
        {"r": label}).scalar()


def run_do_acervo_migrado(sessao: Session) -> int | None:
    """O run sintético do m03. É a âncora de `run_id` de tudo que veio dos CSVs."""
    return run_por_rotulo(sessao, ROTULO_ACERVO_MIGRADO)


def run_aberto_ou_criar(sessao: Session, label: str) -> int:
    """Run corrente para as etapas que gravam resultado. Cria config default se não houver.

    Existe porque as etapas 7 e 8 precisam de um `run_id` para escrever, e na Fase 2 ainda não
    há quem crie run (isso é a Fase 3). Deliberadamente simples: reaproveita o último run
    aberto de mesmo rótulo em vez de acumular um run por execução.
    """
    existente = sessao.execute(
        text("SELECT id FROM run WHERE label = :r AND status = 'open' "
             "ORDER BY id DESC LIMIT 1"), {"r": label}).scalar()
    if existente:
        return existente
    cv = config_versao_por_rotulo(sessao, "default")
    if cv is None:
        cv = criar_config_versao(sessao, "default", notes="criada automaticamente")
    return criar_run(sessao, label, cv)


# ── Prompts ─────────────────────────────────────────────────────────────────────────

def upsert_prompt(sessao: Session, name: str, description: str, capability: str,
                  template: str, version: int = 1, active: bool = True) -> int:
    """Registra o prompt e uma versão. `ux_prompt_ativa` garante no máximo um ativo por name.

    Por isso o UPDATE que desativa as outras vem ANTES do insert: inserir uma segunda ativo
    violaria o índice único parcial e derrubaria a transação inteira.
    """
    sessao.execute(
        text("INSERT INTO prompt (name, description, capability) "
             "VALUES (:n, :d, CAST(:c AS capability)) "
             "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description"),
        {"n": name, "d": description, "c": capability})
    if active:
        sessao.execute(
            text("UPDATE prompt_version SET active = false "
                 "WHERE prompt_name = :n AND active AND version <> :v"),
            {"n": name, "v": version})
    return sessao.execute(
        text("INSERT INTO prompt_version (prompt_name, version, template, active) "
             "VALUES (:n, :v, :t, :a) "
             "ON CONFLICT (prompt_name, version) DO UPDATE "
             "SET template = EXCLUDED.template, active = EXCLUDED.active "
             "RETURNING id"),
        {"n": name, "v": version, "t": template, "a": active},
    ).scalar_one()


def prompt_versao_ativa(sessao: Session, name: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM prompt_version WHERE prompt_name = :n AND active"),
        {"n": name}).scalar()


def template_prompt_ativo(sessao: Session, name: str) -> dict[str, Any] | None:
    """`(id, template)` da versão ativo — é o que `core.prompts_resolver` usa para montar o
    prompt de verdade em tempo de execução. `None` quando o prompt não existe no banco ainda
    (etapa cai no fallback hardcoded de `core/prompts.py`, ver docstring do resolver)."""
    linha = sessao.execute(
        text("SELECT id, template FROM prompt_version WHERE prompt_name = :n AND active"),
        {"n": name}).mappings().first()
    return dict(linha) if linha else None


def listar_prompts(sessao: Session) -> list[dict[str, Any]]:
    """Prompts + versões (docs/06_API_E_WEB.md `GET /api/prompts`) — tela de edição com
    histórico e diff."""
    prompts = {p["name"]: {**p, "versoes": []} for p in sessao.execute(
        text("SELECT name, description, capability FROM prompt ORDER BY name")).mappings().all()}
    for v in sessao.execute(
            text("SELECT id, prompt_name, version, template, active, created_by, created_at, "
                 "       notes FROM prompt_version ORDER BY prompt_name, version DESC")).mappings().all():
        if v["prompt_name"] in prompts:
            prompts[v["prompt_name"]]["versoes"].append(dict(v))
    return list(prompts.values())


def prompt_versoes(sessao: Session, name: str) -> list[dict[str, Any]]:
    linhas = sessao.execute(
        text("SELECT id, prompt_name, version, template, active, created_by, created_at, notes "
             "FROM prompt_version WHERE prompt_name = :n ORDER BY version DESC"),
        {"n": name}).mappings().all()
    return [dict(l) for l in linhas]


def prompt_versao_por_numero(sessao: Session, name: str, version: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, prompt_name, version, template, active, created_by, created_at, notes "
             "FROM prompt_version WHERE prompt_name = :n AND version = :v"),
        {"n": name, "v": version}).mappings().first()
    return dict(linha) if linha else None


def proxima_versao_prompt(sessao: Session, name: str) -> int:
    atual = sessao.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM prompt_version WHERE prompt_name = :n"),
        {"n": name}).scalar_one()
    return atual + 1


def criar_prompt_versao(sessao: Session, name: str, template: str, *,
                        created_by: str | None = None, notes: str | None = None) -> int:
    """Cria uma versão NOVA, inativa (histórico — ADR-007/ADR-014: editar cria versão, nunca
    sobrescreve). `ativar_prompt_versao` é quem promove uma versão o ativo."""
    version = proxima_versao_prompt(sessao, name)
    return sessao.execute(
        text("INSERT INTO prompt_version (prompt_name, version, template, active, created_by, "
             "                           notes) "
             "VALUES (:n, :v, :t, false, :p, :notes) RETURNING id"),
        {"n": name, "v": version, "t": template, "p": created_by, "notes": notes},
    ).scalar_one()


def ativar_prompt_versao(sessao: Session, name: str, version: int) -> bool:
    """Promove `version` o ativo e desativa qualquer outra do mesmo prompt — `ux_prompt_ativa`
    garante no máximo um ativo por nome, então o UPDATE que desativa vem antes do que active."""
    sessao.execute(
        text("UPDATE prompt_version SET active = false WHERE prompt_name = :n AND active"),
        {"n": name})
    linha = sessao.execute(
        text("UPDATE prompt_version SET active = true "
             "WHERE prompt_name = :n AND version = :v RETURNING id"),
        {"n": name, "v": version}).first()
    return linha is not None


def diff_prompt(sessao: Session, name: str, versao_a: int, versao_b: int) -> dict[str, Any]:
    a = prompt_versao_por_numero(sessao, name, versao_a)
    b = prompt_versao_por_numero(sessao, name, versao_b)
    if a is None or b is None:
        raise KeyError(f"prompt {name!r} não tem as duas versões {versao_a}/{versao_b}")
    return {"prompt_name": name, "versao_a": versao_a, "versao_b": versao_b,
            "template_a": a["template"], "template_b": b["template"]}


# ── Providers ──────────────────────────────────────────────────────────────────────

def upsert_provedor(sessao: Session, name: str, capabilities: Sequence[str], base_url: str,
                    *, api_key_ref: str | None = None, default_model: str | None = None,
                    allows_fallback: bool = False, batch_size: int | None = None,
                    rpm_limit: int | None = None,
                    cost_in_per_mtok: float | None = None,
                    cost_out_per_mtok: float | None = None,
                    cost_usd_per_call: float | None = None,
                    active: bool = True) -> None:
    """Cadastro/edição de provider. NÃO toca na chave de API — para isso existe
    `gravar_api_key`, que cifra (ADR-022). Separados de propósito: salvar o formulário sem
    preencher o campo de chave não pode apagar a chave que já está lá."""
    sessao.execute(
        text("INSERT INTO provider (name, capabilities, base_url, api_key_ref, default_model, "
             "                      allows_fallback, batch_size, rpm_limit, "
             "                      cost_in_per_mtok, cost_out_per_mtok, "
             "                      cost_usd_per_call, active) "
             "VALUES (:n, CAST(:c AS capability[]), :u, :k, :m, :f, "
             "        COALESCE(:b, 32), :r, :ci, :co, :cc, :a) "
             "ON CONFLICT (name) DO UPDATE SET "
             "  capabilities = EXCLUDED.capabilities, base_url = EXCLUDED.base_url, "
             "  api_key_ref = EXCLUDED.api_key_ref, default_model = EXCLUDED.default_model, "
             "  allows_fallback = EXCLUDED.allows_fallback, "
             "  batch_size = EXCLUDED.batch_size, rpm_limit = EXCLUDED.rpm_limit, "
             "  cost_in_per_mtok = EXCLUDED.cost_in_per_mtok, "
             "  cost_out_per_mtok = EXCLUDED.cost_out_per_mtok, "
             "  cost_usd_per_call = EXCLUDED.cost_usd_per_call, "
             "  active = EXCLUDED.active, updated_at = now()"),
        {"n": name, "c": list(capabilities), "u": base_url, "k": api_key_ref,
         "m": default_model, "f": allows_fallback, "b": batch_size, "r": rpm_limit,
         "ci": cost_in_per_mtok, "co": cost_out_per_mtok,
         "cc": cost_usd_per_call, "a": active})


def gravar_api_key(sessao: Session, provider: str, api_key: str) -> None:
    """Cifra e grava a chave de API de um provedor (Fase 14, ADR-022). O nome do provedor entra
    como AAD, então o criptograma só decifra na linha dele. Write-only: não existe função que
    devolva a chave em claro para fora de `providers.resolver`."""
    from pesquisa_precos.db import secret as seg

    sessao.execute(
        text("UPDATE provider SET api_key_encrypted = :b, api_key_last4 = :l, "
             "  api_key_key_id = :k, api_key_updated_at = now(), updated_at = now() "
             "WHERE name = :n"),
        {"n": provider, "b": seg.cifrar(api_key, context=provider),
         "l": seg.ultimos4(api_key), "k": seg.key_id_atual()})


def limpar_api_key(sessao: Session, provider: str) -> None:
    """Remove a chave gravada (provedor que deixou de exigir autenticação, ou limpeza antes de
    recadastrar)."""
    sessao.execute(
        text("UPDATE provider SET api_key_encrypted = NULL, api_key_last4 = NULL, "
             "  api_key_key_id = NULL, api_key_updated_at = now(), updated_at = now() "
             "WHERE name = :n"), {"n": provider})


def apontar_capacidade(sessao: Session, capability: str, provider: str,
                       model: str | None = None, fallback: str | None = None) -> None:
    """Quem atende cada capability. Fallback em `embed` é PROIBIDO (ADR-006 §2): cair para
    outro provedor no meio corrompe o espaço vetorial em silêncio."""
    if capability == "embed" and fallback:
        raise ValueError(
            "fallback é proibido na capability 'embed' (ADR-006): trocar de provider no meio "
            "mistura espaços vetoriais. Em 'embed', falhar e parar a step é o comportamento "
            "correto.")
    sessao.execute(
        text("INSERT INTO provider_capability (capability, provider, model, fallback) "
             "VALUES (CAST(:c AS capability), :p, :m, :f) "
             "ON CONFLICT (capability) DO UPDATE SET "
             "  provider = EXCLUDED.provider, model = EXCLUDED.model, "
             "  fallback = EXCLUDED.fallback"),
        {"c": capability, "p": provider, "m": model, "f": fallback})


# ── run_step: ciclo de vida de uma execução (Fase 3, docs/04_FASES.md) ──────────────
#
# Tudo aqui é chamado pelo `runner/` (processo.py, contexto_banco.py), nunca pela etapa em
# si — a etapa só enxerga `RunContext` (docs/03_ETAPAS.md §1). `run.cost_cap_usd` é
# lido por `runner.launcher`, não por aqui.


def run_por_id(sessao: Session, run_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, label, mode, status, config_version_id, cost_cap_usd, cost_usd "
             "FROM run WHERE id = :id"), {"id": run_id}).mappings().first()
    return dict(linha) if linha else None


def obter_ou_criar_run_etapa(sessao: Session, run_id: int, step: str) -> int:
    """Uma linha por `(run_id, step)` — `UNIQUE (run_id, step)` no schema. Reaproveita se já
    existir (ex.: `retomar` volta à mesma linha)."""
    existente = sessao.execute(
        text("SELECT id FROM run_step WHERE run_id = :r AND step = :e"),
        {"r": run_id, "e": step}).scalar()
    if existente is not None:
        return existente
    return sessao.execute(
        text("INSERT INTO run_step (run_id, step) VALUES (:r, :e) RETURNING id"),
        {"r": run_id, "e": step}).scalar_one()


def run_etapa_por_id(sessao: Session, run_etapa_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, run_id, step, status, action, fingerprint, effective_params, "
             "       params_override, total, processed, errors, heartbeat_at, pid, "
             "       cost_usd, metrics, error_message, approved_by, approved_at, "
             "       started_at, finished_at "
             "FROM run_step WHERE id = :id"), {"id": run_etapa_id}).mappings().first()
    return dict(linha) if linha else None


def status_run_etapa(sessao: Session, run_etapa_id: int) -> str | None:
    return sessao.execute(
        text("SELECT status FROM run_step WHERE id = :id"), {"id": run_etapa_id}).scalar()


def gravar_params(sessao: Session, run_etapa_id: int, *,
                  effective_params: dict[str, Any], params_override: dict[str, Any]) -> None:
    """Camadas de docs/03_ETAPAS.md §3, já resolvidas. Gravado ANTES de rodar — `retomar` lê
    `effective_params` daqui e nunca recalcula (ADR-008): um run retomado não pode mudar de
    comportamento porque alguém mexeu na config no meio."""
    import json
    sessao.execute(
        text("UPDATE run_step SET effective_params = CAST(:pe AS jsonb), "
             "                     params_override = CAST(:po AS jsonb) WHERE id = :id"),
        {"pe": json.dumps(effective_params, default=str), "po": json.dumps(params_override),
         "id": run_etapa_id})


def marcar_executando(sessao: Session, run_etapa_id: int, *, action: str, pid: int) -> None:
    sessao.execute(
        text("UPDATE run_step SET status = 'running', action = CAST(:a AS run_action), "
             "                     pid = :pid, heartbeat_at = now(), started_at = now(), "
             "                     error_message = NULL "
             "WHERE id = :id"), {"a": action, "pid": pid, "id": run_etapa_id})


def marcar_concluida(sessao: Session, run_etapa_id: int, *, processed: int, errors: int,
                     metrics: dict[str, Any], fingerprint: str) -> None:
    import json
    sessao.execute(
        text("UPDATE run_step SET status = 'finished', processed = :p, errors = :e, "
             "                     metrics = CAST(:m AS jsonb), fingerprint = :fp, "
             "                     finished_at = now() "
             "WHERE id = :id"),
        {"p": processed, "e": errors, "m": json.dumps(metrics, default=str),
         "fp": fingerprint, "id": run_etapa_id})


def marcar_falhou(sessao: Session, run_etapa_id: int, error_message: str) -> None:
    sessao.execute(
        text("UPDATE run_step SET status = 'failed', error_message = :m, finished_at = now() "
             "WHERE id = :id"), {"m": error_message[:4000], "id": run_etapa_id})


def marcar_cancelada(sessao: Session, run_etapa_id: int) -> None:
    sessao.execute(
        text("UPDATE run_step SET status = 'cancelled', finished_at = now() "
             "WHERE id = :id AND status <> 'cancelled'"), {"id": run_etapa_id})


def solicitar_cancelamento(sessao: Session, run_etapa_id: int) -> bool:
    """Gate/CLI só faz um UPDATE (ADR-005) — não há processo para "avisar" diretamente. O
    subprocesso em execução observa isto no próprio `ctx.cancelado()`, que reconsulta o status
    a cada heartbeat. Só tem efeito sobre uma etapa que ainda está `executando`."""
    linha = sessao.execute(
        text("UPDATE run_step SET status = 'cancelled' "
             "WHERE id = :id AND status = 'running' RETURNING id"),
        {"id": run_etapa_id}).first()
    return linha is not None


def marcar_aguardando_aprovacao(sessao: Session, run_etapa_id: int) -> None:
    sessao.execute(
        text("UPDATE run_step SET status = 'awaiting_approval' WHERE id = :id"),
        {"id": run_etapa_id})


def pular(sessao: Session, run_etapa_id: int, motivo: str | None = None) -> bool:
    """Gate: `pular` (docs/06_API_E_WEB.md §3.2). Só tem efeito partindo de
    `aguardando_aprovacao` — pular uma etapa já em outro estado não faz sentido e é ignorado
    (devolve False) em vez de sobrescrever silenciosamente."""
    linha = sessao.execute(
        text("UPDATE run_step SET status = 'skipped', error_message = :m, finished_at = now() "
             "WHERE id = :id AND status = 'awaiting_approval' RETURNING id"),
        {"m": motivo, "id": run_etapa_id}).first()
    return linha is not None


def abortar_run(sessao: Session, run_id: int) -> bool:
    """`abortar run` (ADR-005: gate não segura lock, run não avança sozinho). Marca o run
    `abortado` e sinaliza cancelamento para a etapa que porventura esteja `executando` — o
    mesmo mecanismo de `solicitar_cancelamento` (o subprocesso observa via `ctx.cancelado()`)."""
    sessao.execute(
        text("UPDATE run_step SET status = 'cancelled' "
             "WHERE run_id = :r AND status = 'running'"), {"r": run_id})
    linha = sessao.execute(
        text("UPDATE run SET status = 'aborted', finished_at = now() "
             "WHERE id = :r AND status = 'open' RETURNING id"), {"r": run_id}).first()
    return linha is not None


def aprovar(sessao: Session, run_etapa_id: int, approved_by: str) -> None:
    sessao.execute(
        text("UPDATE run_step SET approved_by = :p, approved_at = now(), "
             "                     status = 'not_started' "
             "WHERE id = :id"), {"p": approved_by, "id": run_etapa_id})


def atualizar_progresso(sessao: Session, run_etapa_id: int, processed: int,
                        total: int | None = None) -> None:
    if total is None:
        sessao.execute(
            text("UPDATE run_step SET processed = :p WHERE id = :id"),
            {"p": processed, "id": run_etapa_id})
    else:
        sessao.execute(
            text("UPDATE run_step SET processed = :p, total = :t WHERE id = :id"),
            {"p": processed, "t": total, "id": run_etapa_id})


def heartbeat(sessao: Session, run_etapa_id: int, pid: int) -> None:
    sessao.execute(
        text("UPDATE run_step SET heartbeat_at = now(), pid = :pid WHERE id = :id"),
        {"pid": pid, "id": run_etapa_id})


def leases_expiradas(sessao: Session, timeout_s: int) -> list[dict[str, Any]]:
    """`run_step` `executando` cujo `heartbeat_at` não avança há mais que `timeout_s` — o
    processo morreu sem marcar nada (kill -9, queda de máquina). Ver docs/04_FASES.md §Fase 3
    item 3: "lease com expiração devolve à fila o que ficou preso"."""
    linhas = sessao.execute(
        text("SELECT id, run_id, step, pid, heartbeat_at FROM run_step "
             "WHERE status = 'running' "
             "  AND (heartbeat_at IS NULL "
             "       OR heartbeat_at < now() - make_interval(secs => :t))"),
        {"t": timeout_s}).mappings().all()
    return [dict(linha) for linha in linhas]


def liberar_lease_expirada(sessao: Session, run_etapa_id: int) -> None:
    """Devolve à fila: `falhou` com uma mensagem que explica por quê — não `nao_iniciada`
    direto, para não apagar o rastro de que algo morreu. `retomar` resume dali normalmente
    (o checkpoint por unidade de trabalho já garante que nada é perdido nem duplicado)."""
    sessao.execute(
        text("UPDATE run_step SET status = 'failed', "
             "  error_message = 'lease expirada — processo não deu heartbeat a tempo "
             "(provavelmente morto); rode com --action retomar', "
             "  finished_at = now() "
             "WHERE id = :id AND status = 'running'"), {"id": run_etapa_id})


def registrar_log(sessao: Session, run_id: int, step: str | None, level: str, message: str,
                  context: dict[str, Any] | None = None) -> None:
    import json
    sessao.execute(
        text("INSERT INTO run_log (run_id, step, level, message, context) "
             "VALUES (:r, :e, :n, :m, CAST(:c AS jsonb))"),
        {"r": run_id, "e": step, "n": level, "m": message,
         "c": json.dumps(context, default=str) if context else None})


def logs_do_run(sessao: Session, run_id: int, *, step: str | None = None,
                limite: int = 200) -> list[dict[str, Any]]:
    if step is None:
        linhas = sessao.execute(
            text("SELECT id, step, level, message, context, created_at FROM run_log "
                 "WHERE run_id = :r ORDER BY id DESC LIMIT :n"),
            {"r": run_id, "n": limite}).mappings().all()
    else:
        linhas = sessao.execute(
            text("SELECT id, step, level, message, context, created_at FROM run_log "
                 "WHERE run_id = :r AND step = :e ORDER BY id DESC LIMIT :n"),
            {"r": run_id, "e": step, "n": limite}).mappings().all()
    return [dict(linha) for linha in linhas]


def erros_do_run(sessao: Session, run_id: int, *, step: str | None = None,
                 apenas_pendentes: bool = True) -> list[dict[str, Any]]:
    """`item_error` do run — a Fase 4 expõe isto em `GET /api/runs/{id}/errors` para o gate/tela
    de etapa mostrar "reprocessar pendentes" sem o operador abrir o banco."""
    condicoes = "run_id = :r"
    parametros: dict[str, Any] = {"r": run_id}
    if step is not None:
        condicoes += " AND step = :e"
        parametros["e"] = step
    if apenas_pendentes:
        condicoes += " AND NOT resolved"
    linhas = sessao.execute(
        text("SELECT id, step, key, error_type, message, attempts, resolved, created_at "
             f"FROM item_error WHERE {condicoes} ORDER BY id DESC"), parametros).mappings().all()
    return [dict(linha) for linha in linhas]


def _capabilities_como_lista(value: Any) -> list[str]:
    """`capability[]` lido por SQL cru volta como o literal do Postgres (`'{embed,rerank}'`),
    não como lista: o `text()` não passa pelo tipo do ORM, então psycopg entrega a string
    crua. Sem esta normalização, um `list(...)` do outro lado quebra em caracteres — e o dano
    é silencioso (um `for c in capabilities` no template simplesmente não marca nada)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [c for c in str(value).strip("{}").split(",") if c]


def listar_provedores(sessao: Session) -> list[dict[str, Any]]:
    """Registro de `provider`/`provider_capability` (ADR-006), sem probe ao vivo — para o
    resultado da sondagem (`provider_status`), ver `checar_todos_ativos`/`checar_capacidade`
    em `providers.health` (Fase 7)."""
    provedores = {p["name"]: {**p, "capabilities": _capabilities_como_lista(p["capabilities"]),
                              "served_capabilities": []} for p in sessao.execute(
        text("SELECT name, capabilities, base_url, default_model, allows_fallback, active, "
             "       batch_size, rpm_limit, cost_in_per_mtok, cost_out_per_mtok, "
             "       cost_usd_per_call, "
             "       api_key_last4, api_key_key_id, api_key_updated_at, "
             "       (api_key_encrypted IS NOT NULL) AS has_api_key, "
             "       updated_at FROM provider ORDER BY name")).mappings().all()}
    for c in sessao.execute(
            text("SELECT capability, provider, model, fallback FROM provider_capability")
    ).mappings().all():
        if c["provider"] in provedores:
            provedores[c["provider"]]["served_capabilities"].append(dict(c))
    return list(provedores.values())


def capacidade_provedor_info(sessao: Session, capability: str) -> dict[str, Any] | None:
    """Uma capacidade + o `provider` que a atende, já com os campos do adapter (base_url,
    batch_size, custo por Mtok, `api_key_ref`...). `None` quando `provider_capability` ainda
    não tem linha para esta capacidade — quem chama (`providers.resolver`) cai no `.env`.
    """
    linha = sessao.execute(
        text("SELECT cp.capability, cp.provider, cp.model, cp.fallback, "
             "       p.base_url, p.api_key_ref, p.api_key_encrypted, p.default_model, "
             "       p.batch_size, p.rpm_limit, "
             "       p.cost_in_per_mtok, p.cost_out_per_mtok, p.cost_usd_per_call, "
             "       p.allows_fallback, p.active "
             "FROM provider_capability cp JOIN provider p ON p.name = cp.provider "
             "WHERE cp.capability = CAST(:c AS capability) AND p.active"),
        {"c": capability}).mappings().first()
    return dict(linha) if linha else None


def atualizar_status_provedor(sessao: Session, provider: str, healthy: bool,
                              latency_ms: int | None, message: str | None) -> None:
    """Resultado de UMA sondagem (`providers.health`) — `provider_status` é sempre a última
    leitura, não histórico (docs/02_SCHEMA.md §10: PK é só `provider`)."""
    sessao.execute(
        text("INSERT INTO provider_status (provider, healthy, latency_ms, message) "
             "VALUES (:p, :s, :l, :m) "
             "ON CONFLICT (provider) DO UPDATE SET "
             "  healthy = EXCLUDED.healthy, latency_ms = EXCLUDED.latency_ms, "
             "  message = EXCLUDED.message, checked_at = now()"),
        {"p": provider, "s": healthy, "l": latency_ms, "m": message[:500] if message else None})


def status_provedores(sessao: Session) -> list[dict[str, Any]]:
    """Última sondagem de cada provedor — o que a tela/CLI de saúde lê para não ter que
    sondar de novo a cada refresh."""
    linhas = sessao.execute(
        text("SELECT provider, healthy, latency_ms, message, checked_at "
             "FROM provider_status ORDER BY provider")).mappings().all()
    return [dict(l) for l in linhas]


def registrar_erro_item(sessao: Session, run_id: int, step: str, key: str,
                        error_type: str, message: str) -> None:
    """Erro de UMA unidade de trabalho — não derruba a etapa (docs/03_ETAPAS.md §1.1 regra 4).
    Reaproveita a linha em nova tentativa em vez de acumular duplicata por `key`."""
    existente = sessao.execute(
        text("SELECT id, attempts FROM item_error "
             "WHERE run_id = :r AND step = :e AND key = :c AND NOT resolved"),
        {"r": run_id, "e": step, "c": key}).first()
    if existente:
        sessao.execute(
            text("UPDATE item_error SET attempts = attempts + 1, error_type = :t, "
                 "                     message = :m WHERE id = :id"),
            {"t": error_type, "m": message[:4000], "id": existente.id})
    else:
        sessao.execute(
            text("INSERT INTO item_error (run_id, step, key, error_type, message) "
                 "VALUES (:r, :e, :c, :t, :m)"),
            {"r": run_id, "e": step, "c": key, "t": error_type, "m": message[:4000]})


def registrar_llm_chamada(sessao: Session, *, run_id: int | None, step: str | None,
                          capability: str, provider: str, model: str,
                          key: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
                          cost_usd: float = 0.0, duration_ms: int | None = None,
                          success: bool = True, prompt_version_id: int | None = None) -> int:
    """Toda chamada o provedor pago (docs/02_SCHEMA.md §9). É o que sustenta estimativa, teto
    e dashboard de custo — sem isto não há teto que funcione (ADR-004).

    Quem chama isto na prática são os adapters de provedor da Fase 7 (`providers/`); nesta
    fase o mecanismo de contabilidade e teto que a etapa já enxerga é `ctx.gastar(usd)`, que
    incrementa `run_step.cost_usd`/`run.cost_usd` sem o detalhe por chamada.
    """
    return sessao.execute(
        text("INSERT INTO llm_call (run_id, step, capability, provider, model, "
             "  prompt_version_id, key, tokens_in, tokens_out, cost_usd, duration_ms, success) "
             "VALUES (:r, :e, CAST(:cap AS capability), :prov, :mod, :pv, :ch, :ti, :to, "
             "        :cu, :dm, :s) RETURNING id"),
        {"r": run_id, "e": step, "cap": capability, "prov": provider, "mod": model,
         "pv": prompt_version_id, "ch": key, "ti": tokens_in, "to": tokens_out,
         "cu": cost_usd, "dm": duration_ms, "s": success}).scalar_one()


def incrementar_custo(sessao: Session, run_etapa_id: int, run_id: int, usd: float) -> Decimal:
    """Soma `usd` em `run_step.cost_usd` e `run.cost_usd` no mesmo commit da unidade que
    gastou (docs/08_CONVENCOES.md §5.3). Devolve o total acumulado do RUN — é contra ele que
    `run.cost_cap_usd` é comparado, não contra a etapa isolada."""
    sessao.execute(
        text("UPDATE run_step SET cost_usd = cost_usd + :u WHERE id = :id"),
        {"u": usd, "id": run_etapa_id})
    return sessao.execute(
        text("UPDATE run SET cost_usd = cost_usd + :u WHERE id = :r RETURNING cost_usd"),
        {"u": usd, "r": run_id}).scalar_one()


def custo_run(sessao: Session, run_id: int) -> Decimal:
    return sessao.execute(
        text("SELECT cost_usd FROM run WHERE id = :r"), {"r": run_id}).scalar_one()


def custo_resumo(sessao: Session, *, de: str | None = None, ate: str | None = None) -> dict[str, Any]:
    """Dashboard de custo (docs/06_API_E_WEB.md §4.4): por run, por etapa e acumulado no mês.
    `de`/`ate` filtram por `run_step.finished_at`; sem eles, olha o histórico inteiro."""
    condicoes = "run_step.cost_usd > 0"
    parametros: dict[str, Any] = {}
    if de is not None:
        condicoes += " AND (run_step.finished_at IS NULL OR run_step.finished_at >= :de)"
        parametros["de"] = de
    if ate is not None:
        condicoes += " AND (run_step.finished_at IS NULL OR run_step.finished_at <= :ate)"
        parametros["ate"] = ate
    por_run = sessao.execute(
        text("SELECT run.id AS run_id, run.label, SUM(run_step.cost_usd) AS cost_usd "
             f"FROM run_step JOIN run ON run.id = run_step.run_id WHERE {condicoes} "
             "GROUP BY run.id, run.label ORDER BY run.id DESC"), parametros).mappings().all()
    por_etapa = sessao.execute(
        text("SELECT step, SUM(run_step.cost_usd) AS cost_usd "
             f"FROM run_step WHERE {condicoes} GROUP BY step ORDER BY step"),
        parametros).mappings().all()
    por_mes = sessao.execute(
        text("SELECT to_char(date_trunc('month', COALESCE(run_step.finished_at, "
             "                                             run_step.started_at)), "
             "              'YYYY-MM') AS mes, SUM(run_step.cost_usd) AS cost_usd "
             f"FROM run_step WHERE {condicoes} "
             "  AND COALESCE(run_step.finished_at, run_step.started_at) IS NOT NULL "
             "GROUP BY 1 ORDER BY 1 DESC"), parametros).mappings().all()
    total = sessao.execute(
        text(f"SELECT COALESCE(SUM(run_step.cost_usd), 0) FROM run_step WHERE {condicoes}"),
        parametros).scalar_one()
    return {"total_usd": total, "por_run": [dict(l) for l in por_run],
            "por_etapa": [dict(l) for l in por_etapa], "por_mes": [dict(l) for l in por_mes]}


def listar_exports(sessao: Session, *, run_id: int | None = None) -> list[dict[str, Any]]:
    if run_id is None:
        linhas = sessao.execute(
            text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, created_at "
                 "FROM export ORDER BY id DESC")).mappings().all()
    else:
        linhas = sessao.execute(
            text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, created_at FROM export "
                 "WHERE run_id = :r ORDER BY id DESC"), {"r": run_id}).mappings().all()
    return [dict(l) for l in linhas]


def conteudo_export(sessao: Session, export_id: int) -> tuple[bytes | None, str | None]:
    """Os bytes do XLSX e o nome sugerido. Consulta à parte de `export_por_id` de propósito:
    `conteudo` é um `bytea` de dezenas de MB e não pode entrar em toda listagem."""
    linha = sessao.execute(
        text("SELECT conteudo, nome_arquivo FROM export WHERE id = :id"),
        {"id": export_id}).first()
    return (None, None) if linha is None else (linha[0], linha[1])


def export_por_id(sessao: Session, export_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, hash_arquivo, created_at "
             "FROM export WHERE id = :id"), {"id": export_id}).mappings().first()
    return dict(linha) if linha else None


def ultimo_fingerprint_concluido(sessao: Session, step: str) -> str | None:
    """Fingerprint da última execução CONCLUÍDA desta etapa, em qualquer run — é contra isto
    que uma etapa dependente calcula se está `desatualizada` (ADR-009). Não escopado a um
    `run_id` porque `atualizar` é incremental entre runs; a última execução real da etapa é
    sempre a que vale, não a do run corrente."""
    return sessao.execute(
        text("SELECT fingerprint FROM run_step WHERE step = :e AND status = 'finished' "
             "  AND fingerprint IS NOT NULL "
             "ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 1"), {"e": step}).scalar()
