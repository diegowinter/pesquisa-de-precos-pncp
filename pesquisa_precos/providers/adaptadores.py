"""
Adapters concretos das quatro capacidades (Fase 7) — cada um embrulha um cliente já validado
(`llm_curador.Curador`, `embedder_local`/`gpu_remoto`, `ocr_pdf`) atrás dos `Protocol` de
`protocolos.py`, sem reescrever a lógica de chamada.

Nomes seguem docs/04_FASES.md §Fase 7:
  - `gpu_caseira`   → embed/rerank remotos (servidor da GPU caseira, `gpu_remoto`)
  - `lm_studio`     → chat local (OpenAI-compatible)
  - `openrouter`    → chat pago (OpenAI-compatible)
  - `openai_compat` → chat genérico (qualquer servidor OpenAI-compatible além dos dois acima)
  - `ocr_local`     → servidor de OCR (`ocr_pdf`)
Mais dois adapters que não têm nome próprio na doc porque atendem embed/rerank IN-PROCESS
(sem GPU remota) — mesma família de `gpu_caseira`, só que rodando na própria máquina.

Retry/backoff: `Curador` e `ocr_pdf.ocr_pagina` já retriam nativamente. Os clientes remotos de
GPU (`gpu_remoto`) não tinham — os adapters de embed/rerank remoto acrescentam um retry curto
aqui, sem mexer em `gpu_remoto.py` (mesma regra de "não portar sem ler": preservar o cliente
validado e só embrulhar).
"""

import time

import numpy as np

from pesquisa_precos.providers.protocolos import InfoProvedor

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

    def __init__(self, info: InfoProvedor, *, api_key: str, curador_kwargs: dict | None = None):
        from pesquisa_precos.providers.llm_curador import Curador

        self.info = info
        kwargs = dict(curador_kwargs or {})
        # max_retries do próprio cliente OpenAI (honra Retry-After) — Curador já faz isso.
        # `setdefault` porque quem chama (ex.: etapa 3, concorrência alta) pode querer um
        # valor diferente sem colidir com o default daqui.
        kwargs.setdefault("max_retries", 6)
        self.curador = Curador(model=info.modelo, base_url=info.base_url, api_key=api_key,
                               **kwargs)

    def invocar(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage
        resp = self.curador.llm.invoke([HumanMessage(content=prompt)])
        return (resp.content or "").strip()

    def invocar_json(self, prompt: str) -> dict:
        return self.curador._invocar_json(prompt)  # noqa: SLF001 — mesmo pacote, uso interno

    def custo_estimado(self, tokens_in: int, tokens_out: int) -> float:
        if self.info.custo_in_por_mtok is None or self.info.custo_out_por_mtok is None:
            return 0.0
        return (tokens_in / 1_000_000) * self.info.custo_in_por_mtok + \
               (tokens_out / 1_000_000) * self.info.custo_out_por_mtok


class EmbedGpuCaseiraAdapter:
    """`gpu_caseira` (embed) — cliente HTTP do servidor de GPU (`gpu_remoto.EmbedderRemoto`),
    com retry curto por lote. FALLBACK PROIBIDO (ADR-006): se isto falhar após as tentativas,
    a exceção sobe e a etapa para — nunca cair para outro provedor de embedding."""

    def __init__(self, info: InfoProvedor, *, api_key: str, cache_path: str | None = None):
        from pesquisa_precos.providers.gpu_remoto import EmbedderRemoto

        self.info = info
        self._cliente = EmbedderRemoto(info.base_url, api_key, cache_path=cache_path)

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        return _com_retry(self._cliente.embed_textos, textos)

    def salvar_cache(self) -> None:
        self._cliente.salvar_cache()

    def liberar(self) -> None:
        self._cliente.liberar()


class EmbedProcessoAdapter:
    """Embed IN-PROCESS (sentence-transformers, sem GPU remota). Mesma proibição de fallback
    da `EmbedGpuCaseiraAdapter` — aqui não há rede, então não há retry a fazer."""

    def __init__(self, info: InfoProvedor, *, cache_path: str | None = None):
        from pesquisa_precos.providers.embedder_local import EmbedderLocal

        self.info = info
        self._cliente = EmbedderLocal(info.modelo, cache_path=cache_path, batch=info.batch_size)

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        return self._cliente.embed_textos(textos)

    def salvar_cache(self) -> None:
        self._cliente.salvar_cache()

    def liberar(self) -> None:
        self._cliente.liberar()


class RerankGpuCaseiraAdapter:
    """`gpu_caseira` (rerank) — cliente HTTP do servidor de GPU (`gpu_remoto.RerankerRemoto`),
    com retry curto por lote. Fallback é PERMITIDO em `rerank` (ADR-006) — quem decide se usa
    é `resolver.py`/a etapa, não este adapter."""

    def __init__(self, info: InfoProvedor, *, api_key: str):
        from pesquisa_precos.providers.gpu_remoto import RerankerRemoto

        self.info = info
        self._cliente = RerankerRemoto(info.base_url, api_key, batch=info.batch_size)

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        return _com_retry(self._cliente.score_pares, pares)

    def liberar(self) -> None:
        self._cliente.liberar()


class RerankProcessoAdapter:
    """Rerank IN-PROCESS (cross-encoder local, sem GPU remota)."""

    def __init__(self, info: InfoProvedor):
        from pesquisa_precos.providers.reranker_local import RerankerLocal

        self.info = info
        self._cliente = RerankerLocal(info.modelo, batch=info.batch_size)

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        return self._cliente.score_pares(pares)

    def liberar(self) -> None:
        self._cliente.liberar()


class OcrLocalAdapter:
    """`ocr_local` — servidor OCR OpenAI-compatible (`ocr_pdf.ocr_pagina`, já retria)."""

    def __init__(self, info: InfoProvedor, *, api_key: str):
        self.info = info
        self._api_key = api_key

    def ocr_pagina(self, png_bytes: bytes) -> str:
        from pesquisa_precos.providers import ocr_pdf

        return ocr_pdf.ocr_pagina(png_bytes, self.info.base_url, self.info.modelo, self._api_key)


def custo_estimado_generico(info: InfoProvedor, tokens_in: int, tokens_out: int) -> float:
    """Mesma fórmula de `ChatAdapter.custo_estimado`, exposta solta p/ `estimar()` de etapas
    que não têm (e não precisam) instanciar o adapter de verdade (docs/03_ETAPAS.md §1.1
    regra 5: `estimar()` nunca gasta nem chama provedor pago)."""
    if info.custo_in_por_mtok is None or info.custo_out_por_mtok is None:
        return 0.0
    return (tokens_in / 1_000_000) * info.custo_in_por_mtok + \
           (tokens_out / 1_000_000) * info.custo_out_por_mtok
