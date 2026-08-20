"""
Servidor da capacidade `pareamento` (Fase 11, ADR-019) — roda na máquina que tem a GPU.

Recebe catálogo + itens, calcula BM25 e cosseno, aplica o corte top-K + piso e devolve SÓ os
pares sobreviventes. É o que tira `rank-bm25`, `sentence-transformers`/torch e numpy do
container — e, mais importante, tira dele o pico de RAM que definia o tamanho da máquina.

⚠ O corte continua EM STREAMING (ver `core/pareamento/motor.py`). Externalizar move a
restrição de lado, não a remove: a memória desta máquina é tão finita quanto a do container, e
o `MemoryError` de ~33M linhas aconteceria aqui do mesmo jeito se alguém materializasse o
produto cartesiano antes de cortar.

A lógica NÃO vive aqui: está em `core/pareamento/motor.py`, o mesmo módulo que o adapter em
processo usa.

Rodar (na máquina da GPU):
    uv sync --extra localmente
    python servidor_pareamento.py --host 0.0.0.0 --port 8300
    # e no .env de quem orquestra:  PAREAMENTO_BASE_URL=http://<host>:8300
"""

import argparse
import os

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from pesquisa_precos.core.pareamento import motor

app = FastAPI(title="Pareamento catálogo × itens (capacidade `pareamento`)")

_API_KEY = os.getenv("PAREAMENTO_API_KEY", "")
_EMBEDDER_MODEL = os.getenv("EMBEDDER_MODEL", "BAAI/bge-m3")

# O embedder é carregado UMA vez e reaproveitado: subir um modelo de 2 GB para a GPU a cada
# requisição custaria mais que o pareamento inteiro.
_embedder = None


def _obter_embedder():
    global _embedder
    if _embedder is None:
        from pesquisa_precos.providers.embedder_local import EmbedderLocal

        _embedder = EmbedderLocal(_EMBEDDER_MODEL)
    return _embedder


def _conferir_chave(authorization: str | None) -> None:
    if not _API_KEY:
        return
    if authorization != f"Bearer {_API_KEY}":
        raise HTTPException(status_code=401, detail="chave inválida")


@app.get("/health")
def health() -> dict:
    try:
        import torch

        gpu = torch.cuda.is_available()
    except ImportError:
        return {"status": "degradado", "torch": False, "gpu": False}
    return {"status": "ok", "torch": True, "gpu": gpu, "modelo": _EMBEDDER_MODEL}


@app.post("/parear")
async def parear(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _conferir_chave(authorization)
    corpo = await request.json()
    catalogo = corpo.get("catalogo") or []
    itens = corpo.get("itens") or []
    if not catalogo or not itens:
        return {"pares": [], "n_pares": 0}

    # `sem_embedding` existe para rodar só com BM25 (equivalente ao `--sem-embedding` da 6a),
    # útil quando a GPU está indisponível e o recall léxico já basta para um teste.
    embed = None if corpo.get("sem_embedding") else _obter_embedder().embed_textos

    pares = await run_in_threadpool(
        motor.parear, catalogo, itens,
        piso=float(corpo.get("piso", 0.30)),
        top_k=corpo.get("top_k"),
        embed=embed,
    )
    return {"pares": pares, "n_pares": len(pares)}


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8300)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
