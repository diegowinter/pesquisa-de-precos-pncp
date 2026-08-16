"""
Wrapper do cross-encoder bge-reranker-v2-m3 (etapa 6b).

Pontua pares (texto_catalogo, texto_item) e devolve um score por par. Regra de GPU (6GB):
o reranker roda sozinho — carregue no início, libere no fim. fp16 em cuda; cada lado do
par é truncado a ~512 tokens pelo próprio CrossEncoder (max_length).
"""

import numpy as np


class RerankerLocal:
    def __init__(self, model_name: str, batch: int = 16, max_length: int = 512):
        from sentence_transformers import CrossEncoder
        import torch

        self.batch = batch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        kwargs = {"max_length": max_length, "device": device}
        if device == "cuda":
            kwargs["automodel_args"] = {"torch_dtype": torch.float16}
        self.model = CrossEncoder(model_name, **kwargs)

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        """Score (sigmoid do logit) para cada par (texto_catalogo, texto_item)."""
        if not pares:
            return np.zeros(0, dtype=float)
        scores = self.model.predict(
            pares, batch_size=self.batch, show_progress_bar=False,
            convert_to_numpy=True, apply_softmax=False,
        )
        return np.asarray(scores, dtype=float).reshape(-1)

    def liberar(self) -> None:
        try:
            import torch
            del self.model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
