"""
Adapters concretos das capacidades (Fase 7) — cada um embrulha um cliente já validado atrás
dos `Protocol` de `protocolos.py`, sem reescrever a lógica de chamada.

**Todo adapter aqui é CLIENTE DE UM SERVIÇO.** Desde a ADR-021 não existe mais versão "em
processo": trabalho de GPU e de CPU intensiva (embedding, reranking, OCR, parse de PDF, BM25)
vive no repositório `pncp-servicos-locais` e é alcançado por HTTP. Este processo baixa, grava
no banco e conversa — nada mais. Era o mesmo problema do `--fonte csv`: dois caminhos para o
mesmo resultado, e a divergência entre eles não levantava exceção.

Nomes seguem docs/04_FASES.md §Fase 7:
  - `gpu_caseira`   → rerank remoto (serviço `gpu`)
  - `lm_studio`     → chat local (OpenAI-compatible)
  - `openrouter`    → chat pago (OpenAI-compatible)
  - `openai_compat` → chat genérico (qualquer servidor OpenAI-compatible além dos dois acima)

Retry/backoff: `Curador` já retria nativamente. O cliente de GPU (`gpu_remoto`) não tinha — o
adapter de rerank acrescenta um retry curto aqui, sem mexer em `gpu_remoto.py` (regra de "não
portar sem ler": preservar o cliente validado e só embrulhar).
"""

import time

import numpy as np

from pesquisa_precos.providers.protocolos import ProviderInfo

_RETRY_TENTATIVAS = 3
_RETRY_BACKOFF_S = 2.0


def _com_retry(fn, *args, **kwargs):
    ultimo_erro: Exception | None = None
    for tentativa in range(1, _RETRY_TENTATIVAS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — repassa a última após esgotar tentativas
            ultimo_erro = exc
            if tentativa < _RETRY_TENTATIVAS:
                time.sleep(_RETRY_BACKOFF_S * tentativa)
    assert ultimo_erro is not None
    raise ultimo_erro


class ChatAdapter:
    """`lm_studio` / `openrouter` / `openai_compat` — os três são o mesmo protocolo HTTP
    (OpenAI-compatible); o que muda é só base_url/modelo/chave, já resolvidos em `info`."""

    def __init__(self, info: ProviderInfo, *, api_key: str, curador_kwargs: dict | None = None):
        from pesquisa_precos.providers.llm_curador import Curador

        self.info = info
        kwargs = dict(curador_kwargs or {})
        # max_retries do próprio cliente OpenAI (honra Retry-After) — Curador já faz isso.
        # `setdefault` porque quem chama (ex.: etapa 3, concorrência alta) pode querer um
        # valor diferente sem colidir com o default daqui.
        kwargs.setdefault("max_retries", 6)
        self.curador = Curador(model=info.model, base_url=info.base_url, api_key=api_key,
                               **kwargs)

    def invocar(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage
        resp = self.curador.llm.invoke([HumanMessage(content=prompt)])
        return (resp.content or "").strip()

    def invocar_json(self, prompt: str) -> dict:
        return self.curador._invocar_json(prompt)  # noqa: SLF001 — mesmo pacote, uso interno

    def custo_estimado(self, tokens_in: int, tokens_out: int) -> float:
        if self.info.cost_in_per_mtok is None or self.info.cost_out_per_mtok is None:
            return 0.0
        return (tokens_in / 1_000_000) * self.info.cost_in_per_mtok + \
               (tokens_out / 1_000_000) * self.info.cost_out_per_mtok


class EmbedGpuCaseiraAdapter:
    """`gpu_caseira` (embed) — cliente HTTP do servidor de GPU (`gpu_remoto.EmbedderRemoto`),
    com retry curto por lote. FALLBACK PROIBIDO (ADR-006): se isto falhar após as tentativas,
    a exceção sobe e a etapa para — nunca cair para outro provedor de embedding."""

    def __init__(self, info: ProviderInfo, *, api_key: str):
        from pesquisa_precos.providers.gpu_remoto import EmbedderRemoto

        self.info = info
        self._cliente = EmbedderRemoto(info.base_url, api_key)

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        return _com_retry(self._cliente.embed_textos, textos)

    def liberar(self) -> None:
        self._cliente.liberar()


class RerankGpuCaseiraAdapter:
    """`gpu_caseira` (rerank) — cliente HTTP do servidor de GPU (`gpu_remoto.RerankerRemoto`),
    com retry curto por lote. Fallback é PERMITIDO em `rerank` (ADR-006) — quem decide se usa
    é `resolver.py`/a etapa, não este adapter."""

    def __init__(self, info: ProviderInfo, *, api_key: str):
        from pesquisa_precos.providers.gpu_remoto import RerankerRemoto

        self.info = info
        self._cliente = RerankerRemoto(info.base_url, api_key, batch=info.batch_size)

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        return _com_retry(self._cliente.score_pares, pares)

    def liberar(self) -> None:
        self._cliente.liberar()


class PareamentoRemotoAdapter:
    """`pareamento` remoto — BM25 + cosseno + corte, do outro lado de um HTTP."""

    def __init__(self, info: ProviderInfo, *, api_key: str, timeout_s: int = 1800):
        self.info = info
        self._api_key = api_key
        self._timeout = timeout_s

    def parear(self, catalogo: list[dict], itens: list[dict], *,
               piso: float, top_k: int | None = None) -> list[dict]:
        import requests

        resp = requests.post(
            f"{self.info.base_url.rstrip('/')}/parear",
            json={"catalogo": catalogo, "itens": itens, "piso": piso, "top_k": top_k},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=(30, self._timeout),
        )
        resp.raise_for_status()
        return resp.json()["pares"]


def custo_estimado_generico(info: ProviderInfo, tokens_in: int, tokens_out: int) -> float:
    """Mesma fórmula de `ChatAdapter.custo_estimado`, exposta solta p/ `estimar()` de etapas
    que não têm (e não precisam) instanciar o adapter de verdade (docs/03_ETAPAS.md §1.1
    regra 5: `estimar()` nunca gasta nem chama provedor pago)."""
    if info.cost_in_per_mtok is None or info.cost_out_per_mtok is None:
        return 0.0
    return ((tokens_in / 1_000_000) * info.cost_in_per_mtok
            + (tokens_out / 1_000_000) * info.cost_out_per_mtok)
