"""
Parse de PDF (PyMuPDF) + roteamento de OCR para páginas escaneadas (etapa 5.1).

- `extrair_paginas(pdf)`  → texto nativo por página + densidade (chars/página).
- `pagina_escaneada(dens)`→ True se a página tem pouco texto nativo (limiar 100 chars).
- `rasterizar(page)`      → PNG bytes a 200 DPI.
- `ocr_pagina(png)`       → markdown da página via servidor OCR OpenAI-compatible.

Regra crítica (o que estourava o contexto antes): NUNCA enviar o documento inteiro ao OCR
— uma imagem por chamada, uma página por vez.
"""

import base64
import time

import requests

LIMIAR_ESCANEADA = 100  # < 100 chars nativos por página → tratar como escaneada
DPI_OCR = 200

PROMPT_OCR = (
    "Converta o conteúdo desta página de documento para markdown, preservando tabelas, "
    "números e a ordem de leitura. Responda SOMENTE com o markdown, sem comentários."
)


def extrair_paginas(pdf_path: str) -> list[dict]:
    """Abre o PDF e devolve [{pagina, texto, densidade}] (1-indexed). Requer PyMuPDF (fitz)."""
    import fitz  # PyMuPDF

    paginas = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            texto = page.get_text("text") or ""
            paginas.append({
                "pagina": i,
                "texto": texto,
                "densidade": len(texto.strip()),
                "_page_index": i - 1,
            })
    return paginas


def pagina_escaneada(densidade: int) -> bool:
    return densidade < LIMIAR_ESCANEADA


def rasterizar(pdf_path: str, page_index: int, dpi: int = DPI_OCR) -> bytes:
    """Rasteriza uma página específica (0-indexed) a `dpi` e devolve PNG bytes."""
    import fitz

    with fitz.open(pdf_path) as doc:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def ocr_pagina(png_bytes: bytes, base_url: str, model: str, api_key: str = "ocr",
               timeout: int = 300, max_tentativas: int = 4) -> str:
    """
    Envia UMA imagem de página ao servidor de OCR (payload OpenAI-compatible chat/completions
    com image_url base64) e devolve o markdown. Retry com backoff. '' em falha persistente.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT_OCR},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "temperature": 0.0,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for tentativa in range(1, max_tentativas + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=(30, timeout))
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001
            if tentativa == max_tentativas:
                return ""
            time.sleep(min(tentativa * 5, 30))
    return ""
