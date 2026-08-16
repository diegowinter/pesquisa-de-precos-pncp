"""
Clientes HTTP drop-in para o embedder/reranker rodando no servidor de GPU (servidor_gpu.py).

Espelham a interface das versões in-process (embedder_local.EmbedderLocal /
reranker_local.RerankerLocal), então a etapa 6a/6b troca só a linha de instanciação:
  - EmbedderRemoto.embed_textos(list[str]) -> np.ndarray  (L2-normalizado, como o local)
  - RerankerRemoto.score_pares(list[tuple[str,str]]) -> np.ndarray

O cache parquet do embedder fica AQUI, no cliente (mesma lógica do local): só os textos
inéditos vão pela rede. `liberar()` é no-op (o modelo vive no servidor).
"""

import hashlib
import os

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
    """Cliente do /embed. Mesma API do EmbedderLocal (embed_textos/salvar_cache/liberar)."""

    def __init__(self, base_url: str, api_key: str = "gpu", cache_path: str | None = None):
        self.url = base_url.rstrip("/") + "/embed"
        self.api_key = api_key
        self.cache_path = cache_path
        self._cache: dict[str, np.ndarray] = {}
        if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            import pandas as pd
            df = pd.read_parquet(cache_path)
            for h, vec in zip(df["hash"], df["vetor"]):
                self._cache[h] = np.asarray(vec, dtype=np.float32)

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

    def salvar_cache(self) -> None:
        if not self.cache_path or not self._cache:
            return
        import pandas as pd
        os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
        pd.DataFrame({"hash": list(self._cache.keys()),
                      "vetor": [v.tolist() for v in self._cache.values()]}).to_parquet(
            self.cache_path, index=False)

    def liberar(self) -> None:  # o modelo vive no servidor; nada a liberar aqui.
        pass


class RerankerRemoto:
    """Cliente do /rerank. Mesma API do RerankerLocal (score_pares/liberar)."""

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
