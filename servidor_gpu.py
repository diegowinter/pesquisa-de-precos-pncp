"""
Servidor de GPU (embedder bge-m3 + reranker bge-reranker-v2-m3) para rodar no PC da GPU.

Expõe os dois modelos por HTTP, para as etapas 6a (embedder) e 6b (reranker) rodarem de
uma máquina sem GPU, do mesmo jeito que o servidor_ocr.py faz com o OCR. Os clientes ficam
em scripts/gpu_remoto.py (EmbedderRemoto/RerankerRemoto) e falam este protocolo:

    POST /embed   {"textos": [...]}            -> {"vetores": [[...], ...]}   (L2-normalizado)
    POST /rerank  {"pares": [[cat, item], ...]} -> {"scores": [...]}
    GET  /health  -> {"ok": true, "carregado": "embedder|reranker|nenhum"}

VRAM (6 GB): por padrão só UM modelo fica na GPU por vez — ao atender /embed o servidor
libera o reranker e vice-versa (lazy-load + swap). A 6a roda inteira antes da 6b, então o
swap acontece no máximo uma vez. Use --manter-ambos se a GPU comportar os dois juntos.

Rodar no PC da GPU:
    pip install "sentence-transformers" torch fastapi "uvicorn[standard]"
    python servidor_gpu.py --host 0.0.0.0 --port 8100
"""

import argparse
import threading
import time

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="GPU embedder+reranker")

_EMBEDDER_MODEL = "BAAI/bge-m3"
_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
_MANTER_AMBOS = False

_embedder = None
_reranker = None
_lock = threading.Lock()  # serializa carga/descarga e uso (modelos não são thread-safe p/ swap)


def _liberar(qual: str):
    """Descarrega um modelo e limpa a VRAM."""
    global _embedder, _reranker
    try:
        import torch
        if qual == "embedder" and _embedder is not None:
            del _embedder
            _embedder = None
        elif qual == "reranker" and _reranker is not None:
            del _reranker
            _reranker = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as e:  # noqa: BLE001
        print(f"[gpu] erro ao liberar {qual}: {e}")


def _get_embedder():
    global _embedder
    if _embedder is None:
        if not _MANTER_AMBOS:
            _liberar("reranker")
        from sentence_transformers import SentenceTransformer
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[gpu] carregando embedder {_EMBEDDER_MODEL} ({dev})...")
        m = SentenceTransformer(_EMBEDDER_MODEL, device=dev)
        if dev == "cuda":
            m = m.half()
        _embedder = m
    return _embedder


def _get_reranker():
    global _reranker
    if _reranker is None:
        if not _MANTER_AMBOS:
            _liberar("embedder")
        from sentence_transformers import CrossEncoder
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[gpu] carregando reranker {_RERANKER_MODEL} ({dev})...")
        kwargs = {"max_length": 512, "device": dev}
        if dev == "cuda":
            kwargs["automodel_args"] = {"torch_dtype": torch.float16}
        _reranker = CrossEncoder(_RERANKER_MODEL, **kwargs)
    return _reranker


def _embed(textos: list[str]) -> list[list[float]]:
    with _lock:
        m = _get_embedder()
        vet = m.encode(textos, batch_size=32, normalize_embeddings=True,
                       show_progress_bar=False, convert_to_numpy=True)
    return vet.astype("float32").tolist()


def _rerank(pares: list) -> list[float]:
    import numpy as np
    with _lock:
        m = _get_reranker()
        scores = m.predict([(a, b) for a, b in pares], batch_size=16,
                           show_progress_bar=False, convert_to_numpy=True, apply_softmax=False)
    return np.asarray(scores, dtype=float).reshape(-1).tolist()


@app.get("/health")
def health():
    carregado = "embedder" if _embedder is not None else ("reranker" if _reranker is not None else "nenhum")
    return {"ok": True, "carregado": carregado}


@app.post("/embed")
async def embed(request: Request):
    body = await request.json()
    textos = body.get("textos") or []
    if not textos:
        return {"vetores": []}
    vetores = await run_in_threadpool(lambda: _embed(textos))
    return {"vetores": vetores}


@app.post("/rerank")
async def rerank(request: Request):
    body = await request.json()
    pares = body.get("pares") or []
    if not pares:
        return {"scores": []}
    scores = await run_in_threadpool(lambda: _rerank(pares))
    return {"scores": scores}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--embedder-model", default=_EMBEDDER_MODEL)
    ap.add_argument("--reranker-model", default=_RERANKER_MODEL)
    ap.add_argument("--manter-ambos", action="store_true",
                    help="mantém embedder e reranker na VRAM ao mesmo tempo (GPU folgada)")
    ap.add_argument("--preload", choices=["embedder", "reranker", "nenhum"], default="nenhum",
                    help="carrega um modelo no boot (warmup)")
    args = ap.parse_args()
    _EMBEDDER_MODEL = args.embedder_model
    _RERANKER_MODEL = args.reranker_model
    _MANTER_AMBOS = args.manter_ambos
    if args.preload == "embedder":
        _get_embedder()
    elif args.preload == "reranker":
        _get_reranker()
    print(f"[gpu] pronto em http://{args.host}:{args.port}  (/embed, /rerank, /health)")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
