"""
Clientes HTTP do embedder e do reranker que rodam no servidor de GPU (`servicos/gpu.py`, no
repo `pncp-servicos-locais`):

  - EmbedderRemoto.embed_textos(list[str]) -> np.ndarray  (L2-normalizado)
  - RerankerRemoto.score_pares(list[tuple[str, str]]) -> np.ndarray

O embedder guarda em memória, por hash de texto, o que já pediu: dentro de uma execução, texto
repetido não volta pela rede. `liberar()` é no-op — o modelo vive no servidor.
"""

import hashlib

import numpy as np
import requests

_EMBED_CHUNK = 512   # textos por requisição /embed
_RERANK_CHUNK = 256  # pares por requisição /rerank
_TIMEOUT = (30, 600)


def _hash(texto: str) -> str:
    return hashlib.sha1((texto or "").encode("utf-8")).hexdigest()


def _post(url: str, api_key: str, payload: dict) -> dict:
    r = requests.post(url, json=payload,
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


class EmbedderRemoto:
    """Cliente do /embed."""

    def __init__(self, base_url: str, api_key: str = "gpu"):
        self.url = base_url.rstrip("/") + "/embed"
        self.api_key = api_key
        self._cache: dict[str, np.ndarray] = {}

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        hashes = [_hash(t) for t in textos]
        faltantes = {h: t for h, t in zip(hashes, textos) if h not in self._cache}
        if faltantes:
            novos_h = list(faltantes.keys())
            novos_t = list(faltantes.values())
            vetores: list[list[float]] = []
            for i in range(0, len(novos_t), _EMBED_CHUNK):
                resp = _post(self.url, self.api_key, {"textos": novos_t[i:i + _EMBED_CHUNK]})
                vetores.extend(resp["vetores"])
            for h, v in zip(novos_h, vetores):
                self._cache[h] = np.asarray(v, dtype=np.float32)
        return np.vstack([self._cache[h] for h in hashes])

    def liberar(self) -> None:  # o model vive no servidor; nada a liberar aqui.
        pass


class RerankerRemoto:
    """Cliente do /rerank."""

    def __init__(self, base_url: str, api_key: str = "gpu", batch: int = _RERANK_CHUNK):
        self.url = base_url.rstrip("/") + "/rerank"
        self.api_key = api_key
        self.chunk = batch or _RERANK_CHUNK

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        if not pares:
            return np.zeros(0, dtype=float)
        scores: list[float] = []
        for i in range(0, len(pares), self.chunk):
            lote = [[a, b] for a, b in pares[i:i + self.chunk]]
            resp = _post(self.url, self.api_key, {"pares": lote})
            scores.extend(resp["scores"])
        return np.asarray(scores, dtype=float).reshape(-1)

    def liberar(self) -> None:
        pass
