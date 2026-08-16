"""
Servidor de OCR OpenAI-compatível (PaddleOCR + GPU) para rodar no PC da GPU (WSL2).

Fala o MESMO protocolo que o cliente da etapa 5.1 já usa (POST /v1/chat/completions com
uma image_url base64), então o scripts/ocr_pdf.py NÃO precisa de nenhuma alteração — basta
apontar OCR_BASE_URL para este servidor. Recebe UMA imagem de página, roda PaddleOCR na GPU
e devolve o texto reconstruído (ordem de leitura) no formato choices[0].message.content.

Rodar no outro PC (dentro do WSL2, com CUDA):
    pip install "paddleocr==2.7.3" "paddlepaddle-gpu" fastapi "uvicorn[standard]" pillow numpy
    python servidor_ocr.py --host 0.0.0.0 --port 8000
(Ver o passo-a-passo completo de rede/instalação na conversa.)
"""

import argparse
import base64
import io
import queue
import re
import time

import numpy as np
from fastapi import FastAPI, Request
from PIL import Image
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="OCR PaddleOCR (OpenAI-compat)")
_USE_GPU = True   # setado no __main__ a partir de --device
_POOL = None      # fila de instâncias PaddleOCR (uma por worker; o Paddle NÃO é thread-safe).


def _nova_ocr():
    from paddleocr import PaddleOCR
    # lang='pt' cobre português; use_gpu usa CUDA (--device gpu); angle_cls corrige giro.
    return PaddleOCR(use_angle_cls=True, lang="pt", use_gpu=_USE_GPU, show_log=False)


def _init_pool(n: int):
    """Cria N instâncias PaddleOCR numa fila. Requisições concorrentes pegam/devolvem uma
    instância (backpressure natural: se todas ocupadas, a thread espera na fila)."""
    global _POOL
    _POOL = queue.Queue()
    for i in range(n):
        print(f"[ocr] carregando instância {i + 1}/{n}...")
        _POOL.put(_nova_ocr())


def _decodificar_imagem(data_url: str) -> np.ndarray:
    """data:image/png;base64,.... → np.ndarray RGB (aceita também base64 cru sem prefixo)."""
    b64 = re.sub(r"^data:image/[^;]+;base64,", "", data_url.strip())
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    return np.array(img)


def _ocr_para_texto(np_img: np.ndarray) -> str:
    """Roda PaddleOCR e reconstrói o texto em ordem de leitura (top→bottom, left→right).
    Pega uma instância livre do pool e a devolve ao final (permite N OCRs concorrentes)."""
    ocr = _POOL.get()
    try:
        resultado = ocr.ocr(np_img, cls=True)
    finally:
        _POOL.put(ocr)
    if not resultado or not resultado[0]:
        return ""
    linhas = []  # (y_topo, x_esq, texto)
    for box, (texto, conf) in resultado[0]:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        linhas.append((min(ys), min(xs), texto))
    # Agrupa por faixa vertical (tolerância) para preservar linhas de tabela na ordem certa.
    linhas.sort(key=lambda t: (round(t[0] / 10), t[1]))
    return "\n".join(t[2] for t in linhas)


def _extrair_data_url(body: dict) -> str | None:
    """Puxa a primeira image_url do payload OpenAI (messages[].content[].image_url.url)."""
    for msg in body.get("messages", []):
        conteudo = msg.get("content")
        if isinstance(conteudo, list):
            for parte in conteudo:
                if parte.get("type") == "image_url":
                    url = (parte.get("image_url") or {}).get("url")
                    if url:
                        return url
    return None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    data_url = _extrair_data_url(body)
    texto = ""
    if data_url:
        try:
            # Offload do OCR (bloqueante) para a threadpool: assim várias requisições rodam
            # concorrentes sem travar o event loop; o pool de instâncias limita a N reais.
            texto = await run_in_threadpool(
                lambda: _ocr_para_texto(_decodificar_imagem(data_url)))
        except Exception as e:  # noqa: BLE001
            texto = ""
            print(f"[ocr] erro: {e}")
    return {
        "id": "ocr-paddle",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "paddle"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": texto}}],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--device", choices=["gpu", "cpu"], default="gpu",
                    help="gpu (CUDA) ou cpu (fallback se a GPU segfaultar)")
    ap.add_argument("--workers", type=int, default=3,
                    help="nº de instâncias PaddleOCR no pool (OCRs concorrentes). "
                         "Na RTX 3050 (6GB) cabem ~3. Case a concorrência do cliente com este valor.")
    args = ap.parse_args()
    _USE_GPU = args.device == "gpu"
    print(f"[ocr] carregando PaddleOCR ({args.device.upper()}) — {args.workers} instância(s)...")
    _init_pool(args.workers)  # warmup: cria/baixa os modelos antes de aceitar conexões
    print(f"[ocr] pronto em http://{args.host}:{args.port}/v1/chat/completions")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
