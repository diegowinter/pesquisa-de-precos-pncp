"""
Contratos de provedor (Fase 7, ADR-006) — quatro capacidades: chat, embed, rerank, ocr.

Cada `Protocol` é o que uma etapa pode assumir de QUALQUER adapter daquela capacidade,
independente de quem atende de verdade (GPU caseira, LM Studio, OpenRouter, um servidor
OpenAI-compatible genérico ou um servidor de OCR). A etapa nunca importa uma classe concreta
de `adaptadores.py` — só recebe um objeto que satisfaz o `Protocol` via `ctx.providers`.

`ProviderInfo` é o espelho em memória de uma linha `provider` + `provider_capability`
(docs/02_SCHEMA.md §10): cada adapter carrega a sua para poder responder "quanto isso custa"
e "quantos por lote" sem que a etapa precise saber de onde esses números vieram.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class ProviderInfo:
    """Metadados de UM provedor resolvido para UMA capacidade — não é a linha crua do banco,
    é o que o adapter precisa para operar e para se descrever (dashboard, estimativa)."""

    name: str
    capability: str  # 'chat' | 'embed' | 'rerank' | 'ocr'
    base_url: str
    model: str
    batch_size: int = 32
    rpm_limit: int | None = None
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None
    # Preço médio de UMA chamada (Fase 14). `None` = não informado: o `estimar()` responde
    # "não estimado" em vez de inventar. `0.0` é o provedor local, que é grátis.
    cost_usd_per_call: float | None = None
    # Fallback é PROIBIDO em 'embed' (ADR-006 §2) — `resolver.py` já recusa montar um
    # `ProviderInfo` de embed com fallback preenchido; o campo existe para chat/rerank/ocr.
    fallback_provider: str | None = None


@runtime_checkable
class ChatProvider(Protocol):
    """Capacidade `chat` — completions de texto (classificação, extração guiada, comparação)."""

    info: ProviderInfo

    def invocar(self, prompt: str) -> str:
        """Devolve o texto da resposta. Levanta em falha de infraestrutura (a etapa decide
        se isso é erro de item ou motivo de abortar — ver docs/03_ETAPAS.md §1.1 regra 4)."""

    def invocar_json(self, prompt: str) -> dict:
        """Como `invocar`, mas parseia a resposta como JSON (com 1 retry em JSON inválido)."""

    def custo_estimado(self, tokens_in: int, tokens_out: int) -> float:
        """USD para essa quantidade de tokens, pelo preço por Mtok deste provider. 0.0 quando
        não há preço configurado (provedor local não custa dinheiro)."""


@runtime_checkable
class EmbedProvider(Protocol):
    """Capacidade `embed` — vetores para o score semântico da etapa 6a.

    FALLBACK PROIBIDO (ADR-006 §2): se este provedor falhar, a etapa deve parar, nunca cair
    para outro — trocar de provedor no meio mistura espaços vetoriais em silêncio.
    """

    info: ProviderInfo

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        """Vetores L2-normalizados, na ordem de `textos`."""

    def liberar(self) -> None:
        """Libera o modelo/conexão ao final da etapa."""


@runtime_checkable
class RerankProvider(Protocol):
    """Capacidade `rerank` — score de cross-encoder por par (catálogo, item) da etapa 6b."""

    info: ProviderInfo

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        """Score por par, na ordem de `pares`."""

    def liberar(self) -> None:
        """Libera o modelo/conexão ao final da etapa."""


@runtime_checkable
class MatchingProvider(Protocol):
    """Capacidade `pareamento` (Fase 11, ADR-019) — catálogo × itens → pares sobreviventes.

    Recebe os textos, calcula BM25 e cosseno, aplica o corte top-K + piso EM STREAMING e
    devolve só o que sobreviveu. O corte continua sendo em streaming — mudou de lado, não
    desapareceu: materializar o produto cartesiano num DataFrame já causou `MemoryError` real
    com ~33M linhas, e isso vale igual dentro do serviço.
    """

    info: ProviderInfo

    def parear(self, catalogo: list[dict], itens: list[dict], *,
               piso: float, top_k: int | None = None) -> list[dict]:
        """`[{codigo, item_key, categoria, score_bm25, score_cosseno}]` — só sobreviventes."""
