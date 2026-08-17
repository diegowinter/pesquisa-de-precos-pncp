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


# ── Provedores ──────────────────────────────────────────────────────────────────────

def upsert_provedor(sessao: Session, nome: str, capacidades: Sequence[str], base_url: str,
                    *, api_key_ref: str | None = None, modelo_padrao: str | None = None,
                    permite_fallback: bool = False) -> None:
    """`api_key_ref` guarda o NOME da variável de ambiente. A chave NUNCA vai ao banco (§5.10)."""
    sessao.execute(
        text("INSERT INTO provedor (nome, capacidades, base_url, api_key_ref, "
             "                      modelo_padrao, permite_fallback) "
             "VALUES (:n, CAST(:c AS capacidade[]), :u, :k, :m, :f) "
             "ON CONFLICT (nome) DO UPDATE SET "
             "  capacidades = EXCLUDED.capacidades, base_url = EXCLUDED.base_url, "
             "  api_key_ref = EXCLUDED.api_key_ref, modelo_padrao = EXCLUDED.modelo_padrao, "
             "  permite_fallback = EXCLUDED.permite_fallback, atualizado_em = now()"),
        {"n": nome, "c": list(capacidades), "u": base_url, "k": api_key_ref,
         "m": modelo_padrao, "f": permite_fallback})


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
