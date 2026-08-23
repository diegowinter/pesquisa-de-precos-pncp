"""
Resolução de prompt em tempo de execução (Fase 6, docs/04_FASES.md — "prompts migrados de
`core/prompts.py` para o banco, com versão ativa e histórico").

`core/prompts.py` continua existindo e é o FALLBACK: se a `prompt_versao` ativa não existir no
banco (banco não semeado, ou testes sem banco), a etapa continua funcionando exatamente como
antes, com o texto hardcoded. Isso é deliberado — ADR-014 manda o *texto* do prompt para o
banco, não o mecanismo de chamada, e a etapa não pode quebrar por falta de seed.

`carregar_ativos()` é chamada UMA vez, na thread principal da etapa, antes de qualquer worker
subir — nunca dentro de um pool de threads/`_tls`. `Session` do SQLAlchemy não é thread-safe
(docs/08_CONVENCOES.md §5.3 é sobre outra coisa, mas o princípio é o mesmo: uma sessão, um
dono); os workers recebem só o dict já resolvido (`{nome: (template, prompt_versao_id)}`), que
é imutável e barato de repassar por `Curador.from_provedor(..., prompts_ativos=...)`.

O "template" gravado em `prompt_versao.template` é uma string `str.format()` com placeholders
nomeados (`{descricao}`, `{janela_texto}`, ...) — quem monta os valores dinâmicos (blocos de
categoria renderizados de `categorias.py`, texto condicional de unidade) continua sendo código
Python em `providers/llm_curador.py`, só o TEXTO fixo do prompt vira dado. Chaves `{` literais
do JSON de exemplo pedido na resposta precisam estar escapadas (`{{`/`}}`) no template.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pesquisa_precos.db.repos import execution as repo

PromptsAtivos = dict[str, tuple[str, int]]


def carregar_ativos(sessao: Session, nomes: list[str]) -> PromptsAtivos:
    """`{nome: (template, prompt_versao_id)}` só para os prompts que TÊM versão ativa no
    banco. Nome ausente do dict = sem seed ainda = fallback hardcoded."""
    ativos: PromptsAtivos = {}
    for nome in nomes:
        linha = repo.template_prompt_ativo(sessao, nome)
        if linha is not None:
            ativos[nome] = (linha["template"], linha["id"])
    return ativos


def resolver(ativos: PromptsAtivos | None, nome: str, fallback_texto: str,
            **valores: Any) -> tuple[str, int | None]:
    """Devolve `(texto_do_prompt, prompt_versao_id)`. `prompt_versao_id` é `None` quando o
    fallback foi usado — é o valor que `llm_chamada.prompt_version_id` deve gravar (Fase 7:
    quem grava a chamada lê isto do resultado do `Curador`, não recalcula)."""
    if not ativos or nome not in ativos:
        return fallback_texto, None
    template, prompt_versao_id = ativos[nome]
    try:
        return template.format(**valores), prompt_versao_id
    except (KeyError, IndexError, ValueError):
        # Template do banco mal formado (placeholder errado) não pode derrubar a etapa —
        # cai no fallback do código, do mesmo jeito que "prompt não semeado".
        return fallback_texto, None
