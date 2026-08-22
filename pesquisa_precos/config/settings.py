"""
Configuração de BOOTSTRAP (`.env`) — o que a aplicação precisa saber antes de conseguir ler o
banco.

Até a Fase 14 este módulo era a configuração inteira da pipeline: modelo, base_url, chave de
API, thresholds, custos. A ADR-022 mudou a fronteira para **configurável vs. bootstrap**: tudo
que é ajuste de operação foi para o banco e se edita pela tela (`/provedores` e o formulário de
cada etapa, gerado do `Params`), versionado em `config_versao`.

Sobraram duas variáveis, e nenhuma delas se troca pela tela:
  - `DATABASE_URL` — resolvida em `db/sessao.py`, não aqui;
  - `APP_SECRET_KEY` — a chave-mestra que cifra os segredos do banco (`db/segredo.py`). Uma
    chave não pode morar dentro do que ela protege.

`carregar_config()` sobrevive porque `ContextoExecucao.config` ainda a expõe às etapas, mas
devolve um dict praticamente vazio. Se você veio aqui procurar onde configurar um modelo ou um
threshold: é na tela, não neste arquivo.
"""

import os

from dotenv import load_dotenv

from pesquisa_precos.config.paths import RAIZ

# O `.env` sempre morou na raiz do projeto. Antes da Fase 0 ela era deduzida da profundidade
# deste arquivo (`parent.parent`); agora vem de `paths.RAIZ`, que não muda quando um módulo é
# movido de lugar. Carregar do lugar errado degradaria em silêncio: `carregar_config()` tem
# default para tudo, então a pipeline rodaria com modelo/URL/threshold errados sem avisar.
# O segundo load é o fallback histórico para um `.env` um nível acima (herdado de quando este
# projeto era uma subpasta de `itens-via-script`). `load_dotenv` não sobrescreve o que já foi
# definido, então o `.env` da raiz continua tendo precedência.
load_dotenv(RAIZ / ".env")
load_dotenv(RAIZ.parent / ".env")


def _f(nome: str, default: float) -> float:
    try:
        return float(os.getenv(nome, default))
    except (TypeError, ValueError):
        return default


def _i(nome: str, default: int) -> int:
    try:
        return int(os.getenv(nome, default))
    except (TypeError, ValueError):
        return default


def carregar_config() -> dict:
    """O que ainda vem do `.env`. Quase nada, por desenho (ADR-022).

    Continua devolvendo um dict — e não `None` — porque `ctx.config` faz parte do contrato de
    etapa (docs/03_ETAPAS.md §1) e várias delas ainda recebem o parâmetro sem usá-lo. Quando o
    último uso sumir, o campo sai do contexto junto.
    """
    return {}
