"""
Repositório de execução e configuração (`run`, `run_etapa`, `config_versao`, `config_valor`,
`prompt`, `prompt_versao`, `provedor`).

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


def criar_config_versao(sessao: Session, rotulo: str, criado_por: str | None = None,
                        notas: str | None = None) -> int:
    """Config é IMUTÁVEL: editar cria versão nova (ADR-014). Isto só cria."""
    return sessao.execute(
        text("INSERT INTO config_versao (rotulo, criado_por, notas) "
             "VALUES (:r, :p, :n) RETURNING id"),
        {"r": rotulo, "p": criado_por, "n": notas},
    ).scalar_one()


def config_versao_por_rotulo(sessao: Session, rotulo: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM config_versao WHERE rotulo = :r ORDER BY id DESC LIMIT 1"),
        {"r": rotulo}).scalar()


def gravar_config(sessao: Session, config_versao_id: int, valores: dict[str, Any]) -> int:
    """Valores de uma versão de config. `valor` é `jsonb` — número é número, não string."""
    import json
    n = 0
    for chave, valor in valores.items():
        n += sessao.execute(
            text("INSERT INTO config_valor (config_versao_id, chave, valor) "
                 "VALUES (:v, :c, CAST(:j AS jsonb)) "
                 "ON CONFLICT (config_versao_id, chave) DO UPDATE SET valor = EXCLUDED.valor"),
            {"v": config_versao_id, "c": chave, "j": json.dumps(valor)},
        ).rowcount
    return n


def ler_config(sessao: Session, config_versao_id: int) -> dict[str, Any]:
    return {c: v for c, v in sessao.execute(
        text("SELECT chave, valor FROM config_valor WHERE config_versao_id = :v"),
        {"v": config_versao_id}).all()}


def listar_config_versoes(sessao: Session) -> list[dict[str, Any]]:
    """Todas as versões, mais recente primeiro — tela de configuração (docs/06_API_E_WEB.md
    §4.5). Config é IMUTÁVEL (ADR-014): esta lista é o histórico completo, nunca sobrescrito."""
    linhas = sessao.execute(
        text("SELECT id, rotulo, criado_por, criado_em, notas FROM config_versao "
             "ORDER BY id DESC")).mappings().all()
    return [dict(linha) for linha in linhas]


def config_versao_por_id(sessao: Session, config_versao_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, rotulo, criado_por, criado_em, notas FROM config_versao "
             "WHERE id = :id"), {"id": config_versao_id}).mappings().first()
    if linha is None:
        return None
    dado = dict(linha)
    dado["valores"] = ler_config(sessao, config_versao_id)
    return dado


def diff_config(sessao: Session, id_a: int, id_b: int) -> dict[str, Any]:
    """Diferença chave a chave entre duas `config_versao` (docs/06_API_E_WEB.md
    `GET /api/config/versoes/{id}/diff/{outro_id}`). Só reporta chaves que mudaram — chave
    ausente de um lado aparece com valor `None` do lado que não a define."""
    valores_a = ler_config(sessao, id_a)
    valores_b = ler_config(sessao, id_b)
    chaves = sorted(set(valores_a) | set(valores_b))
    diferencas = [
        {"chave": c, "de": valores_a.get(c), "para": valores_b.get(c)}
        for c in chaves if valores_a.get(c) != valores_b.get(c)
    ]
    return {"config_versao_a": id_a, "config_versao_b": id_b, "diferencas": diferencas}


def criar_run(sessao: Session, rotulo: str, config_versao_id: int, *,
              modo: str = "assistido", status: str = "aberto",
              criado_por: str | None = None) -> int:
    return sessao.execute(
        text("INSERT INTO run (rotulo, modo, status, config_versao_id, criado_por, "
             "                 concluido_em) "
             "VALUES (:r, CAST(:m AS modo_run), CAST(:s AS status_run), :cv, :p, "
             "        CASE WHEN :s = 'concluido' THEN now() ELSE NULL END) "
             "RETURNING id"),
        {"r": rotulo, "m": modo, "s": status, "cv": config_versao_id, "p": criado_por},
    ).scalar_one()


def run_por_rotulo(sessao: Session, rotulo: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM run WHERE rotulo = :r ORDER BY id DESC LIMIT 1"),
        {"r": rotulo}).scalar()


def run_do_acervo_migrado(sessao: Session) -> int | None:
    """O run sintético do m03. É a âncora de `run_id` de tudo que veio dos CSVs."""
    return run_por_rotulo(sessao, ROTULO_ACERVO_MIGRADO)


def run_aberto_ou_criar(sessao: Session, rotulo: str) -> int:
    """Run corrente para as etapas que gravam resultado. Cria config default se não houver.

    Existe porque as etapas 7 e 8 precisam de um `run_id` para escrever, e na Fase 2 ainda não
    há quem crie run (isso é a Fase 3). Deliberadamente simples: reaproveita o último run
    aberto de mesmo rótulo em vez de acumular um run por execução.
    """
    existente = sessao.execute(
        text("SELECT id FROM run WHERE rotulo = :r AND status = 'aberto' "
             "ORDER BY id DESC LIMIT 1"), {"r": rotulo}).scalar()
    if existente:
        return existente
    cv = config_versao_por_rotulo(sessao, "default")
    if cv is None:
        cv = criar_config_versao(sessao, "default", notas="criada automaticamente")
    return criar_run(sessao, rotulo, cv)


# ── Prompts ─────────────────────────────────────────────────────────────────────────

def upsert_prompt(sessao: Session, nome: str, descricao: str, capacidade: str,
                  template: str, versao: int = 1, ativa: bool = True) -> int:
    """Registra o prompt e uma versão. `ux_prompt_ativa` garante no máximo uma ativa por nome.

    Por isso o UPDATE que desativa as outras vem ANTES do insert: inserir uma segunda ativa
    violaria o índice único parcial e derrubaria a transação inteira.
    """
    sessao.execute(
        text("INSERT INTO prompt (nome, descricao, capacidade) "
             "VALUES (:n, :d, CAST(:c AS capacidade)) "
             "ON CONFLICT (nome) DO UPDATE SET descricao = EXCLUDED.descricao"),
        {"n": nome, "d": descricao, "c": capacidade})
    if ativa:
        sessao.execute(
            text("UPDATE prompt_versao SET ativa = false "
                 "WHERE prompt_nome = :n AND ativa AND versao <> :v"),
            {"n": nome, "v": versao})
    return sessao.execute(
        text("INSERT INTO prompt_versao (prompt_nome, versao, template, ativa) "
             "VALUES (:n, :v, :t, :a) "
             "ON CONFLICT (prompt_nome, versao) DO UPDATE "
             "SET template = EXCLUDED.template, ativa = EXCLUDED.ativa "
             "RETURNING id"),
        {"n": nome, "v": versao, "t": template, "a": ativa},
    ).scalar_one()


def prompt_versao_ativa(sessao: Session, nome: str) -> int | None:
    return sessao.execute(
        text("SELECT id FROM prompt_versao WHERE prompt_nome = :n AND ativa"),
        {"n": nome}).scalar()


def template_prompt_ativo(sessao: Session, nome: str) -> dict[str, Any] | None:
    """`(id, template)` da versão ativa — é o que `core.prompts_resolver` usa para montar o
    prompt de verdade em tempo de execução. `None` quando o prompt não existe no banco ainda
    (etapa cai no fallback hardcoded de `core/prompts.py`, ver docstring do resolver)."""
    linha = sessao.execute(
        text("SELECT id, template FROM prompt_versao WHERE prompt_nome = :n AND ativa"),
        {"n": nome}).mappings().first()
    return dict(linha) if linha else None


def listar_prompts(sessao: Session) -> list[dict[str, Any]]:
    """Prompts + versões (docs/06_API_E_WEB.md `GET /api/prompts`) — tela de edição com
    histórico e diff."""
    prompts = {p["nome"]: {**p, "versoes": []} for p in sessao.execute(
        text("SELECT nome, descricao, capacidade FROM prompt ORDER BY nome")).mappings().all()}
    for v in sessao.execute(
            text("SELECT id, prompt_nome, versao, template, ativa, criado_por, criado_em, "
                 "       notas FROM prompt_versao ORDER BY prompt_nome, versao DESC")).mappings().all():
        if v["prompt_nome"] in prompts:
            prompts[v["prompt_nome"]]["versoes"].append(dict(v))
    return list(prompts.values())


def prompt_versoes(sessao: Session, nome: str) -> list[dict[str, Any]]:
    linhas = sessao.execute(
        text("SELECT id, prompt_nome, versao, template, ativa, criado_por, criado_em, notas "
             "FROM prompt_versao WHERE prompt_nome = :n ORDER BY versao DESC"),
        {"n": nome}).mappings().all()
    return [dict(l) for l in linhas]


def prompt_versao_por_numero(sessao: Session, nome: str, versao: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, prompt_nome, versao, template, ativa, criado_por, criado_em, notas "
             "FROM prompt_versao WHERE prompt_nome = :n AND versao = :v"),
        {"n": nome, "v": versao}).mappings().first()
    return dict(linha) if linha else None


def proxima_versao_prompt(sessao: Session, nome: str) -> int:
    atual = sessao.execute(
        text("SELECT COALESCE(MAX(versao), 0) FROM prompt_versao WHERE prompt_nome = :n"),
        {"n": nome}).scalar_one()
    return atual + 1


def criar_prompt_versao(sessao: Session, nome: str, template: str, *,
                        criado_por: str | None = None, notas: str | None = None) -> int:
    """Cria uma versão NOVA, inativa (histórico — ADR-007/ADR-014: editar cria versão, nunca
    sobrescreve). `ativar_prompt_versao` é quem promove uma versão a ativa."""
    versao = proxima_versao_prompt(sessao, nome)
    return sessao.execute(
        text("INSERT INTO prompt_versao (prompt_nome, versao, template, ativa, criado_por, "
             "                           notas) "
             "VALUES (:n, :v, :t, false, :p, :notas) RETURNING id"),
        {"n": nome, "v": versao, "t": template, "p": criado_por, "notas": notas},
    ).scalar_one()


def ativar_prompt_versao(sessao: Session, nome: str, versao: int) -> bool:
    """Promove `versao` a ativa e desativa qualquer outra do mesmo prompt — `ux_prompt_ativa`
    garante no máximo uma ativa por nome, então o UPDATE que desativa vem antes do que ativa."""
    sessao.execute(
        text("UPDATE prompt_versao SET ativa = false WHERE prompt_nome = :n AND ativa"),
        {"n": nome})
    linha = sessao.execute(
        text("UPDATE prompt_versao SET ativa = true "
             "WHERE prompt_nome = :n AND versao = :v RETURNING id"),
        {"n": nome, "v": versao}).first()
    return linha is not None


def diff_prompt(sessao: Session, nome: str, versao_a: int, versao_b: int) -> dict[str, Any]:
    a = prompt_versao_por_numero(sessao, nome, versao_a)
    b = prompt_versao_por_numero(sessao, nome, versao_b)
    if a is None or b is None:
        raise KeyError(f"prompt {nome!r} não tem as duas versões {versao_a}/{versao_b}")
    return {"prompt_nome": nome, "versao_a": versao_a, "versao_b": versao_b,
            "template_a": a["template"], "template_b": b["template"]}


# ── Provedores ──────────────────────────────────────────────────────────────────────

def upsert_provedor(sessao: Session, nome: str, capacidades: Sequence[str], base_url: str,
                    *, api_key_ref: str | None = None, modelo_padrao: str | None = None,
                    permite_fallback: bool = False, batch_size: int | None = None,
                    rpm_limite: int | None = None,
                    custo_in_por_mtok: float | None = None,
                    custo_out_por_mtok: float | None = None,
                    ativo: bool = True) -> None:
    """Cadastro/edição de provedor. NÃO toca na chave de API — para isso existe
    `gravar_api_key`, que cifra (ADR-022). Separados de propósito: salvar o formulário sem
    preencher o campo de chave não pode apagar a chave que já está lá."""
    sessao.execute(
        text("INSERT INTO provedor (nome, capacidades, base_url, api_key_ref, modelo_padrao, "
             "                      permite_fallback, batch_size, rpm_limite, "
             "                      custo_in_por_mtok, custo_out_por_mtok, ativo) "
             "VALUES (:n, CAST(:c AS capacidade[]), :u, :k, :m, :f, "
             "        COALESCE(:b, 32), :r, :ci, :co, :a) "
             "ON CONFLICT (nome) DO UPDATE SET "
             "  capacidades = EXCLUDED.capacidades, base_url = EXCLUDED.base_url, "
             "  api_key_ref = EXCLUDED.api_key_ref, modelo_padrao = EXCLUDED.modelo_padrao, "
             "  permite_fallback = EXCLUDED.permite_fallback, "
             "  batch_size = EXCLUDED.batch_size, rpm_limite = EXCLUDED.rpm_limite, "
             "  custo_in_por_mtok = EXCLUDED.custo_in_por_mtok, "
             "  custo_out_por_mtok = EXCLUDED.custo_out_por_mtok, "
             "  ativo = EXCLUDED.ativo, atualizado_em = now()"),
        {"n": nome, "c": list(capacidades), "u": base_url, "k": api_key_ref,
         "m": modelo_padrao, "f": permite_fallback, "b": batch_size, "r": rpm_limite,
         "ci": custo_in_por_mtok, "co": custo_out_por_mtok, "a": ativo})


def gravar_api_key(sessao: Session, provedor: str, api_key: str) -> None:
    """Cifra e grava a chave de API de um provedor (Fase 14, ADR-022). O nome do provedor entra
    como AAD, então o criptograma só decifra na linha dele. Write-only: não existe função que
    devolva a chave em claro para fora de `providers.resolver`."""
    from pesquisa_precos.db import segredo as seg

    sessao.execute(
        text("UPDATE provedor SET api_key_cifrada = :b, api_key_last4 = :l, "
             "  api_key_key_id = :k, api_key_atualizada_em = now(), atualizado_em = now() "
             "WHERE nome = :n"),
        {"n": provedor, "b": seg.cifrar(api_key, contexto=provedor),
         "l": seg.ultimos4(api_key), "k": seg.key_id_atual()})


def limpar_api_key(sessao: Session, provedor: str) -> None:
    """Remove a chave gravada (provedor que deixou de exigir autenticação, ou limpeza antes de
    recadastrar)."""
    sessao.execute(
        text("UPDATE provedor SET api_key_cifrada = NULL, api_key_last4 = NULL, "
             "  api_key_key_id = NULL, api_key_atualizada_em = now(), atualizado_em = now() "
             "WHERE nome = :n"), {"n": provedor})


def apontar_capacidade(sessao: Session, capacidade: str, provedor: str,
                       modelo: str | None = None, fallback: str | None = None) -> None:
    """Quem atende cada capacidade. Fallback em `embed` é PROIBIDO (ADR-006 §2): cair para
    outro provedor no meio corrompe o espaço vetorial em silêncio."""
    if capacidade == "embed" and fallback:
        raise ValueError(
            "fallback é proibido na capacidade 'embed' (ADR-006): trocar de provedor no meio "
            "mistura espaços vetoriais. Em 'embed', falhar e parar a etapa é o comportamento "
            "correto.")
    sessao.execute(
        text("INSERT INTO capacidade_provedor (capacidade, provedor, modelo, fallback) "
             "VALUES (CAST(:c AS capacidade), :p, :m, :f) "
             "ON CONFLICT (capacidade) DO UPDATE SET "
             "  provedor = EXCLUDED.provedor, modelo = EXCLUDED.modelo, "
             "  fallback = EXCLUDED.fallback"),
        {"c": capacidade, "p": provedor, "m": modelo, "f": fallback})


# ── run_etapa: ciclo de vida de uma execução (Fase 3, docs/04_FASES.md) ──────────────
#
# Tudo aqui é chamado pelo `runner/` (processo.py, contexto_banco.py), nunca pela etapa em
# si — a etapa só enxerga `ContextoExecucao` (docs/03_ETAPAS.md §1). `run.teto_custo_usd` é
# lido por `runner.executor`, não por aqui.


def run_por_id(sessao: Session, run_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, rotulo, modo, status, config_versao_id, teto_custo_usd, custo_usd "
             "FROM run WHERE id = :id"), {"id": run_id}).mappings().first()
    return dict(linha) if linha else None


def obter_ou_criar_run_etapa(sessao: Session, run_id: int, etapa: str) -> int:
    """Uma linha por `(run_id, etapa)` — `UNIQUE (run_id, etapa)` no schema. Reaproveita se já
    existir (ex.: `retomar` volta à mesma linha)."""
    existente = sessao.execute(
        text("SELECT id FROM run_etapa WHERE run_id = :r AND etapa = :e"),
        {"r": run_id, "e": etapa}).scalar()
    if existente is not None:
        return existente
    return sessao.execute(
        text("INSERT INTO run_etapa (run_id, etapa) VALUES (:r, :e) RETURNING id"),
        {"r": run_id, "e": etapa}).scalar_one()


def run_etapa_por_id(sessao: Session, run_etapa_id: int) -> dict[str, Any] | None:
    linha = sessao.execute(
        text("SELECT id, run_id, etapa, status, acao, fingerprint, params_efetivos, "
             "       params_override, total, processados, erros, heartbeat_em, pid, "
             "       custo_usd, metricas, mensagem_erro, aprovado_por, aprovado_em, "
             "       iniciada_em, concluida_em "
             "FROM run_etapa WHERE id = :id"), {"id": run_etapa_id}).mappings().first()
    return dict(linha) if linha else None


def status_run_etapa(sessao: Session, run_etapa_id: int) -> str | None:
    return sessao.execute(
        text("SELECT status FROM run_etapa WHERE id = :id"), {"id": run_etapa_id}).scalar()


def gravar_params(sessao: Session, run_etapa_id: int, *,
                  params_efetivos: dict[str, Any], params_override: dict[str, Any]) -> None:
    """Camadas de docs/03_ETAPAS.md §3, já resolvidas. Gravado ANTES de rodar — `retomar` lê
    `params_efetivos` daqui e nunca recalcula (ADR-008): um run retomado não pode mudar de
    comportamento porque alguém mexeu na config no meio."""
    import json
    sessao.execute(
        text("UPDATE run_etapa SET params_efetivos = CAST(:pe AS jsonb), "
             "                     params_override = CAST(:po AS jsonb) WHERE id = :id"),
        {"pe": json.dumps(params_efetivos, default=str), "po": json.dumps(params_override),
         "id": run_etapa_id})


def marcar_executando(sessao: Session, run_etapa_id: int, *, acao: str, pid: int) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET status = 'executando', acao = CAST(:a AS acao_execucao), "
             "                     pid = :pid, heartbeat_em = now(), iniciada_em = now(), "
             "                     mensagem_erro = NULL "
             "WHERE id = :id"), {"a": acao, "pid": pid, "id": run_etapa_id})


def marcar_concluida(sessao: Session, run_etapa_id: int, *, processados: int, erros: int,
                     metricas: dict[str, Any], fingerprint: str) -> None:
    import json
    sessao.execute(
        text("UPDATE run_etapa SET status = 'concluida', processados = :p, erros = :e, "
             "                     metricas = CAST(:m AS jsonb), fingerprint = :fp, "
             "                     concluida_em = now() "
             "WHERE id = :id"),
        {"p": processados, "e": erros, "m": json.dumps(metricas, default=str),
         "fp": fingerprint, "id": run_etapa_id})


def marcar_falhou(sessao: Session, run_etapa_id: int, mensagem_erro: str) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET status = 'falhou', mensagem_erro = :m, concluida_em = now() "
             "WHERE id = :id"), {"m": mensagem_erro[:4000], "id": run_etapa_id})


def marcar_cancelada(sessao: Session, run_etapa_id: int) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET status = 'cancelada', concluida_em = now() "
             "WHERE id = :id AND status <> 'cancelada'"), {"id": run_etapa_id})


def solicitar_cancelamento(sessao: Session, run_etapa_id: int) -> bool:
    """Gate/CLI só faz um UPDATE (ADR-005) — não há processo para "avisar" diretamente. O
    subprocesso em execução observa isto no próprio `ctx.cancelado()`, que reconsulta o status
    a cada heartbeat. Só tem efeito sobre uma etapa que ainda está `executando`."""
    linha = sessao.execute(
        text("UPDATE run_etapa SET status = 'cancelada' "
             "WHERE id = :id AND status = 'executando' RETURNING id"),
        {"id": run_etapa_id}).first()
    return linha is not None


def marcar_aguardando_aprovacao(sessao: Session, run_etapa_id: int) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET status = 'aguardando_aprovacao' WHERE id = :id"),
        {"id": run_etapa_id})


def pular(sessao: Session, run_etapa_id: int, motivo: str | None = None) -> bool:
    """Gate: `pular` (docs/06_API_E_WEB.md §3.2). Só tem efeito partindo de
    `aguardando_aprovacao` — pular uma etapa já em outro estado não faz sentido e é ignorado
    (devolve False) em vez de sobrescrever silenciosamente."""
    linha = sessao.execute(
        text("UPDATE run_etapa SET status = 'pulada', mensagem_erro = :m, concluida_em = now() "
             "WHERE id = :id AND status = 'aguardando_aprovacao' RETURNING id"),
        {"m": motivo, "id": run_etapa_id}).first()
    return linha is not None


def abortar_run(sessao: Session, run_id: int) -> bool:
    """`abortar run` (ADR-005: gate não segura lock, run não avança sozinho). Marca o run
    `abortado` e sinaliza cancelamento para a etapa que porventura esteja `executando` — o
    mesmo mecanismo de `solicitar_cancelamento` (o subprocesso observa via `ctx.cancelado()`)."""
    sessao.execute(
        text("UPDATE run_etapa SET status = 'cancelada' "
             "WHERE run_id = :r AND status = 'executando'"), {"r": run_id})
    linha = sessao.execute(
        text("UPDATE run SET status = 'abortado', concluido_em = now() "
             "WHERE id = :r AND status = 'aberto' RETURNING id"), {"r": run_id}).first()
    return linha is not None


def aprovar(sessao: Session, run_etapa_id: int, aprovado_por: str) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET aprovado_por = :p, aprovado_em = now(), "
             "                     status = 'nao_iniciada' "
             "WHERE id = :id"), {"p": aprovado_por, "id": run_etapa_id})


def atualizar_progresso(sessao: Session, run_etapa_id: int, processados: int,
                        total: int | None = None) -> None:
    if total is None:
        sessao.execute(
            text("UPDATE run_etapa SET processados = :p WHERE id = :id"),
            {"p": processados, "id": run_etapa_id})
    else:
        sessao.execute(
            text("UPDATE run_etapa SET processados = :p, total = :t WHERE id = :id"),
            {"p": processados, "t": total, "id": run_etapa_id})


def heartbeat(sessao: Session, run_etapa_id: int, pid: int) -> None:
    sessao.execute(
        text("UPDATE run_etapa SET heartbeat_em = now(), pid = :pid WHERE id = :id"),
        {"pid": pid, "id": run_etapa_id})


def leases_expiradas(sessao: Session, timeout_s: int) -> list[dict[str, Any]]:
    """`run_etapa` `executando` cujo `heartbeat_em` não avança há mais que `timeout_s` — o
    processo morreu sem marcar nada (kill -9, queda de máquina). Ver docs/04_FASES.md §Fase 3
    item 3: "lease com expiração devolve à fila o que ficou preso"."""
    linhas = sessao.execute(
        text("SELECT id, run_id, etapa, pid, heartbeat_em FROM run_etapa "
             "WHERE status = 'executando' "
             "  AND (heartbeat_em IS NULL "
             "       OR heartbeat_em < now() - make_interval(secs => :t))"),
        {"t": timeout_s}).mappings().all()
    return [dict(linha) for linha in linhas]


def liberar_lease_expirada(sessao: Session, run_etapa_id: int) -> None:
    """Devolve à fila: `falhou` com uma mensagem que explica por quê — não `nao_iniciada`
    direto, para não apagar o rastro de que algo morreu. `retomar` resume dali normalmente
    (o checkpoint por unidade de trabalho já garante que nada é perdido nem duplicado)."""
    sessao.execute(
        text("UPDATE run_etapa SET status = 'falhou', "
             "  mensagem_erro = 'lease expirada — processo não deu heartbeat a tempo "
             "(provavelmente morto); rode com --acao retomar', "
             "  concluida_em = now() "
             "WHERE id = :id AND status = 'executando'"), {"id": run_etapa_id})


def registrar_log(sessao: Session, run_id: int, etapa: str | None, nivel: str, mensagem: str,
                  contexto: dict[str, Any] | None = None) -> None:
    import json
    sessao.execute(
        text("INSERT INTO run_log (run_id, etapa, nivel, mensagem, contexto) "
             "VALUES (:r, :e, :n, :m, CAST(:c AS jsonb))"),
        {"r": run_id, "e": etapa, "n": nivel, "m": mensagem,
         "c": json.dumps(contexto, default=str) if contexto else None})


def logs_do_run(sessao: Session, run_id: int, *, etapa: str | None = None,
                limite: int = 200) -> list[dict[str, Any]]:
    if etapa is None:
        linhas = sessao.execute(
            text("SELECT id, etapa, nivel, mensagem, contexto, criado_em FROM run_log "
                 "WHERE run_id = :r ORDER BY id DESC LIMIT :n"),
            {"r": run_id, "n": limite}).mappings().all()
    else:
        linhas = sessao.execute(
            text("SELECT id, etapa, nivel, mensagem, contexto, criado_em FROM run_log "
                 "WHERE run_id = :r AND etapa = :e ORDER BY id DESC LIMIT :n"),
            {"r": run_id, "e": etapa, "n": limite}).mappings().all()
    return [dict(linha) for linha in linhas]


def erros_do_run(sessao: Session, run_id: int, *, etapa: str | None = None,
                 apenas_pendentes: bool = True) -> list[dict[str, Any]]:
    """`erro_item` do run — a Fase 4 expõe isto em `GET /api/runs/{id}/erros` para o gate/tela
    de etapa mostrar "reprocessar pendentes" sem o operador abrir o banco."""
    condicoes = "run_id = :r"
    parametros: dict[str, Any] = {"r": run_id}
    if etapa is not None:
        condicoes += " AND etapa = :e"
        parametros["e"] = etapa
    if apenas_pendentes:
        condicoes += " AND NOT resolvido"
    linhas = sessao.execute(
        text("SELECT id, etapa, chave, tipo_erro, mensagem, tentativas, resolvido, criado_em "
             f"FROM erro_item WHERE {condicoes} ORDER BY id DESC"), parametros).mappings().all()
    return [dict(linha) for linha in linhas]


def listar_provedores(sessao: Session) -> list[dict[str, Any]]:
    """Registro de `provedor`/`capacidade_provedor` (ADR-006), sem probe ao vivo — para o
    resultado da sondagem (`provedor_status`), ver `checar_todos_ativos`/`checar_capacidade`
    em `providers.saude` (Fase 7)."""
    provedores = {p["nome"]: {**p, "capacidades_atendidas": []} for p in sessao.execute(
        text("SELECT nome, capacidades, base_url, modelo_padrao, permite_fallback, ativo, "
             "       batch_size, rpm_limite, custo_in_por_mtok, custo_out_por_mtok, "
             "       api_key_last4, api_key_key_id, api_key_atualizada_em, "
             "       (api_key_cifrada IS NOT NULL) AS tem_api_key, "
             "       atualizado_em FROM provedor ORDER BY nome")).mappings().all()}
    for c in sessao.execute(
            text("SELECT capacidade, provedor, modelo, fallback FROM capacidade_provedor")
    ).mappings().all():
        if c["provedor"] in provedores:
            provedores[c["provedor"]]["capacidades_atendidas"].append(dict(c))
    return list(provedores.values())


def capacidade_provedor_info(sessao: Session, capacidade: str) -> dict[str, Any] | None:
    """Uma capacidade + o `provedor` que a atende, já com os campos do adapter (base_url,
    batch_size, custo por Mtok, `api_key_ref`...). `None` quando `capacidade_provedor` ainda
    não tem linha para esta capacidade — quem chama (`providers.resolver`) cai no `.env`.
    """
    linha = sessao.execute(
        text("SELECT cp.capacidade, cp.provedor, cp.modelo, cp.fallback, "
             "       p.base_url, p.api_key_ref, p.api_key_cifrada, p.modelo_padrao, "
             "       p.batch_size, p.rpm_limite, "
             "       p.custo_in_por_mtok, p.custo_out_por_mtok, p.permite_fallback, p.ativo "
             "FROM capacidade_provedor cp JOIN provedor p ON p.nome = cp.provedor "
             "WHERE cp.capacidade = CAST(:c AS capacidade) AND p.ativo"),
        {"c": capacidade}).mappings().first()
    return dict(linha) if linha else None


def atualizar_status_provedor(sessao: Session, provedor: str, saudavel: bool,
                              latencia_ms: int | None, mensagem: str | None) -> None:
    """Resultado de UMA sondagem (`providers.saude`) — `provedor_status` é sempre a última
    leitura, não histórico (docs/02_SCHEMA.md §10: PK é só `provedor`)."""
    sessao.execute(
        text("INSERT INTO provedor_status (provedor, saudavel, latencia_ms, mensagem) "
             "VALUES (:p, :s, :l, :m) "
             "ON CONFLICT (provedor) DO UPDATE SET "
             "  saudavel = EXCLUDED.saudavel, latencia_ms = EXCLUDED.latencia_ms, "
             "  mensagem = EXCLUDED.mensagem, verificado_em = now()"),
        {"p": provedor, "s": saudavel, "l": latencia_ms, "m": mensagem[:500] if mensagem else None})


def status_provedores(sessao: Session) -> list[dict[str, Any]]:
    """Última sondagem de cada provedor — o que a tela/CLI de saúde lê para não ter que
    sondar de novo a cada refresh."""
    linhas = sessao.execute(
        text("SELECT provedor, saudavel, latencia_ms, mensagem, verificado_em "
             "FROM provedor_status ORDER BY provedor")).mappings().all()
    return [dict(l) for l in linhas]


def registrar_erro_item(sessao: Session, run_id: int, etapa: str, chave: str,
                        tipo_erro: str, mensagem: str) -> None:
    """Erro de UMA unidade de trabalho — não derruba a etapa (docs/03_ETAPAS.md §1.1 regra 4).
    Reaproveita a linha em nova tentativa em vez de acumular duplicata por `chave`."""
    existente = sessao.execute(
        text("SELECT id, tentativas FROM erro_item "
             "WHERE run_id = :r AND etapa = :e AND chave = :c AND NOT resolvido"),
        {"r": run_id, "e": etapa, "c": chave}).first()
    if existente:
        sessao.execute(
            text("UPDATE erro_item SET tentativas = tentativas + 1, tipo_erro = :t, "
                 "                     mensagem = :m WHERE id = :id"),
            {"t": tipo_erro, "m": mensagem[:4000], "id": existente.id})
    else:
        sessao.execute(
            text("INSERT INTO erro_item (run_id, etapa, chave, tipo_erro, mensagem) "
                 "VALUES (:r, :e, :c, :t, :m)"),
            {"r": run_id, "e": etapa, "c": chave, "t": tipo_erro, "m": mensagem[:4000]})


def registrar_llm_chamada(sessao: Session, *, run_id: int | None, etapa: str | None,
                          capacidade: str, provedor: str, modelo: str,
                          chave: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
                          custo_usd: float = 0.0, duracao_ms: int | None = None,
                          sucesso: bool = True, prompt_versao_id: int | None = None) -> int:
    """Toda chamada a provedor pago (docs/02_SCHEMA.md §9). É o que sustenta estimativa, teto
    e dashboard de custo — sem isto não há teto que funcione (ADR-004).

    Quem chama isto na prática são os adapters de provedor da Fase 7 (`providers/`); nesta
    fase o mecanismo de contabilidade e teto que a etapa já enxerga é `ctx.gastar(usd)`, que
    incrementa `run_etapa.custo_usd`/`run.custo_usd` sem o detalhe por chamada.
    """
    return sessao.execute(
        text("INSERT INTO llm_chamada (run_id, etapa, capacidade, provedor, modelo, "
             "  prompt_versao_id, chave, tokens_in, tokens_out, custo_usd, duracao_ms, sucesso) "
             "VALUES (:r, :e, CAST(:cap AS capacidade), :prov, :mod, :pv, :ch, :ti, :to, "
             "        :cu, :dm, :s) RETURNING id"),
        {"r": run_id, "e": etapa, "cap": capacidade, "prov": provedor, "mod": modelo,
         "pv": prompt_versao_id, "ch": chave, "ti": tokens_in, "to": tokens_out,
         "cu": custo_usd, "dm": duracao_ms, "s": sucesso}).scalar_one()


def incrementar_custo(sessao: Session, run_etapa_id: int, run_id: int, usd: float) -> Decimal:
    """Soma `usd` em `run_etapa.custo_usd` e `run.custo_usd` no mesmo commit da unidade que
    gastou (docs/08_CONVENCOES.md §5.3). Devolve o total acumulado do RUN — é contra ele que
    `run.teto_custo_usd` é comparado, não contra a etapa isolada."""
    sessao.execute(
        text("UPDATE run_etapa SET custo_usd = custo_usd + :u WHERE id = :id"),
        {"u": usd, "id": run_etapa_id})
    return sessao.execute(
        text("UPDATE run SET custo_usd = custo_usd + :u WHERE id = :r RETURNING custo_usd"),
        {"u": usd, "r": run_id}).scalar_one()


def custo_run(sessao: Session, run_id: int) -> Decimal:
    return sessao.execute(
        text("SELECT custo_usd FROM run WHERE id = :r"), {"r": run_id}).scalar_one()


def custo_resumo(sessao: Session, *, de: str | None = None, ate: str | None = None) -> dict[str, Any]:
    """Dashboard de custo (docs/06_API_E_WEB.md §4.4): por run, por etapa e acumulado no mês.
    `de`/`ate` filtram por `run_etapa.concluida_em`; sem eles, olha o histórico inteiro."""
    condicoes = "run_etapa.custo_usd > 0"
    parametros: dict[str, Any] = {}
    if de is not None:
        condicoes += " AND (run_etapa.concluida_em IS NULL OR run_etapa.concluida_em >= :de)"
        parametros["de"] = de
    if ate is not None:
        condicoes += " AND (run_etapa.concluida_em IS NULL OR run_etapa.concluida_em <= :ate)"
        parametros["ate"] = ate
    por_run = sessao.execute(
        text("SELECT run.id AS run_id, run.rotulo, SUM(run_etapa.custo_usd) AS custo_usd "
             f"FROM run_etapa JOIN run ON run.id = run_etapa.run_id WHERE {condicoes} "
             "GROUP BY run.id, run.rotulo ORDER BY run.id DESC"), parametros).mappings().all()
    por_etapa = sessao.execute(
        text("SELECT etapa, SUM(run_etapa.custo_usd) AS custo_usd "
             f"FROM run_etapa WHERE {condicoes} GROUP BY etapa ORDER BY etapa"),
        parametros).mappings().all()
    por_mes = sessao.execute(
        text("SELECT to_char(date_trunc('month', COALESCE(run_etapa.concluida_em, "
             "                                             run_etapa.iniciada_em)), "
             "              'YYYY-MM') AS mes, SUM(run_etapa.custo_usd) AS custo_usd "
             f"FROM run_etapa WHERE {condicoes} "
             "  AND COALESCE(run_etapa.concluida_em, run_etapa.iniciada_em) IS NOT NULL "
             "GROUP BY 1 ORDER BY 1 DESC"), parametros).mappings().all()
    total = sessao.execute(
        text(f"SELECT COALESCE(SUM(run_etapa.custo_usd), 0) FROM run_etapa WHERE {condicoes}"),
        parametros).scalar_one()
    return {"total_usd": total, "por_run": [dict(l) for l in por_run],
            "por_etapa": [dict(l) for l in por_etapa], "por_mes": [dict(l) for l in por_mes]}


def listar_exports(sessao: Session, *, run_id: int | None = None) -> list[dict[str, Any]]:
    if run_id is None:
        linhas = sessao.execute(
            text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, criado_em "
                 "FROM export ORDER BY id DESC")).mappings().all()
    else:
        linhas = sessao.execute(
            text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, criado_em FROM export "
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
        text("SELECT id, run_id, tipo, arquivo, n_linhas, n_codigos, hash_arquivo, criado_em "
             "FROM export WHERE id = :id"), {"id": export_id}).mappings().first()
    return dict(linha) if linha else None


def ultimo_fingerprint_concluido(sessao: Session, etapa: str) -> str | None:
    """Fingerprint da última execução CONCLUÍDA desta etapa, em qualquer run — é contra isto
    que uma etapa dependente calcula se está `desatualizada` (ADR-009). Não escopado a um
    `run_id` porque `atualizar` é incremental entre runs; a última execução real da etapa é
    sempre a que vale, não a do run corrente."""
    return sessao.execute(
        text("SELECT fingerprint FROM run_etapa WHERE etapa = :e AND status = 'concluida' "
             "  AND fingerprint IS NOT NULL "
             "ORDER BY concluida_em DESC NULLS LAST, id DESC LIMIT 1"), {"e": etapa}).scalar()
