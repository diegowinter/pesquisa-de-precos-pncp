"""
Wrapper do embedder bge-m3 (sentence-transformers) com cache em parquet por hash de texto.

Etapa 6a usa isto para o score semântico (cosseno). Regra de GPU (6GB): o embedder roda
sozinho — carregue no início do script, libere no fim. Device auto: cuda se disponível,
senão cpu; fp16 em cuda.

O cache evita reembeddar textos repetidos entre execuções (chave = sha1 do texto). As
etapas leem/gravam o mesmo parquet (`data/checkpoints/6a_emb_*.parquet`).
"""

import hashlib
import os

import numpy as np
import pandas as pd


def _hash(texto: str) -> str:
    return hashlib.sha1((texto or "").encode("utf-8")).hexdigest()


class EmbedderLocal:
    def __init__(self, model_name: str, cache_path: str | None = None, batch: int = 32):
        from sentence_transformers import SentenceTransformer
        import torch

        self.batch = batch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(model_name, device=self.device)
        if self.device == "cuda":
            self.model = self.model.half()
        self.cache_path = cache_path
        self._cache: dict[str, np.ndarray] = {}
        if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            df = pd.read_parquet(cache_path)
            for h, vec in zip(df["hash"], df["vetor"]):
                self._cache[h] = np.asarray(vec, dtype=np.float32)

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        """Devolve embeddings normalizados (L2) na ordem de `textos`, usando/atualizando o cache."""
        hashes = [_hash(t) for t in textos]
        faltantes = {h: t for h, t in zip(hashes, textos) if h not in self._cache}
        if faltantes:
            novos_h = list(faltantes.keys())
            novos_t = list(faltantes.values())
            vetores = self.model.encode(
                novos_t, batch_size=self.batch, normalize_embeddings=True,
                show_progress_bar=False, convert_to_numpy=True,
            ).astype(np.float32)
            for h, v in zip(novos_h, vetores):
                self._cache[h] = v
        return np.vstack([self._cache[h] for h in hashes])

    def salvar_cache(self) -> None:
        if not self.cache_path or not self._cache:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
        df = pd.DataFrame({
            "hash": list(self._cache.keys()),
            "vetor": [v.tolist() for v in self._cache.values()],
        })
        df.to_parquet(self.cache_path, index=False)

    def liberar(self) -> None:
        """Descarrega o modelo e limpa a VRAM (para a próxima etapa poder usar a GPU)."""
        try:
            import torch
            del self.model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def cosseno_linhas(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosseno par a par entre linhas de a e b (ambos já normalizados L2)."""
    return np.sum(a * b, axis=1)
