"""
Servidor da capacidade `pdf` (Fase 11, ADR-019) — roda na máquina que tem PyMuPDF e a GPU.

Recebe a referência de um documento do PNCP, **baixa o PDF ele mesmo**, extrai o texto nativo
por página, detecta as páginas escaneadas, rasteriza e chama o OCR por dentro. Devolve só
texto. O container que orquestra nunca vê um byte de PDF — nem em disco, nem em memória — e
por isso não precisa de `pymupdf` instalado nem da banda de download.

Por que o OCR fica DENTRO daqui, em vez de o container intermediar: `ocr_pdf.rasterizar()`
depende do PyMuPDF, então tirar o fitz do container quebraria o OCR junto (ADR-019). Além
disso, devolver PNGs de 200 DPI para o container repassar faria o servidor trafegar imagens
sem nenhum uso próprio para elas. A regra crítica "uma imagem por chamada de OCR, nunca o
documento inteiro" passa a ser responsabilidade de quem tem o documento em mãos.

A lógica de extração NÃO vive aqui: está em `pesquisa_precos/providers/pdf_pipeline.py`, o
mesmo módulo que o adapter em processo usa. Duas implementações divergindo dariam textos
diferentes conforme o serviço estivesse no ar — variação de qualidade de extração, não erro.

Rodar (na máquina da GPU):
    uv sync --extra localmente          # pymupdf entra aqui, não no container
    python servidor_pdf.py --host 0.0.0.0 --port 8200
    # e no .env de quem orquestra:  PDF_BASE_URL=http://<host>:8200

Autenticação: `PDF_API_KEY` (Bearer). Vazio = aberto — aceitável num túnel fechado, NUNCA
num endereço público: quem chamar isto faz o servidor baixar URLs arbitrárias.
"""

import argparse
import os

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from pesquisa_precos.providers import ocr_pdf, pdf_pipeline

app = FastAPI(title="Extração de PDF do PNCP (capacidade `pdf`)")

_API_KEY = os.getenv("PDF_API_KEY", "")
# O OCR é do ponto de vista DESTE servidor: por padrão o servidor de OCR roda na mesma
# máquina, então localhost é o default certo aqui (e seria o errado no container).
_OCR_BASE_URL = os.getenv("OCR_BASE_URL", "http://localhost:8000/v1")
_OCR_MODEL = os.getenv("OCR_MODEL", "")
_OCR_API_KEY = os.getenv("OCR_API_KEY", "ocr")


def _conferir_chave(authorization: str | None) -> None:
    if not _API_KEY:
        return
    if authorization != f"Bearer {_API_KEY}":
        raise HTTPException(status_code=401, detail="chave inválida")


def _ocr(png_bytes: bytes) -> str:
    if not _OCR_MODEL:
        return ""   # sem OCR configurado, a página escaneada fica com o texto nativo
    return ocr_pdf.ocr_pagina(png_bytes, _OCR_BASE_URL, _OCR_MODEL, _OCR_API_KEY)


@app.get("/health")
def health() -> dict:
    """O que o health check pré-play do `runner.executor` consulta antes de soltar a etapa 5."""
    try:
        import fitz  # noqa: F401 — só a presença importa

        pymupdf_ok = True
    except ImportError:
        pymupdf_ok = False
    return {"status": "ok" if pymupdf_ok else "degradado",
            "pymupdf": pymupdf_ok,
            "ocr_configurado": bool(_OCR_MODEL)}


@app.post("/extrair")
async def extrair(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Documento → texto por página. Uma requisição = um documento.

    Roda em threadpool: a extração é CPU-bound (PyMuPDF) e seguraria o event loop, fazendo o
    servidor parar de responder ao `/health` justamente enquanto está ocupado — que é quando
    a resposta mais importa.
    """
    _conferir_chave(authorization)
    corpo = await request.json()
    url = corpo.get("url_pncp") or ""
    if not url and not corpo.get("numero_sequencial"):
        raise HTTPException(status_code=400,
                            detail="informe url_pncp ou os identificadores do documento")

    return await run_in_threadpool(
        pdf_pipeline.extrair_documento,
        url,
        numero_controle=corpo.get("numero_controle") or "",
        tipo_doc=corpo.get("tipo_doc") or "",
        numero_sequencial=corpo.get("numero_sequencial"),
        numero_sequencial_ata=corpo.get("numero_sequencial_ata"),
        orgao_cnpj=corpo.get("orgao_cnpj"),
        ano=corpo.get("ano"),
        ocr=_ocr,
        pular_ocr=bool(corpo.get("pular_ocr")),
    )


@app.post("/rasterizar")
async def rasterizar(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Páginas como PNG base64, para a estratégia `visao`. Rota de exceção — ver ADR-010."""
    import base64

    _conferir_chave(authorization)
    corpo = await request.json()
    imagens = await run_in_threadpool(
        pdf_pipeline.rasterizar_documento,
        corpo.get("url_pncp") or "",
        numero_controle=corpo.get("numero_controle") or "",
        tipo_doc=corpo.get("tipo_doc") or "",
        numero_sequencial=corpo.get("numero_sequencial"),
        numero_sequencial_ata=corpo.get("numero_sequencial_ata"),
        orgao_cnpj=corpo.get("orgao_cnpj"),
        ano=corpo.get("ano"),
        max_paginas=corpo.get("max_paginas"),
    )
    return {"paginas_png": [base64.b64encode(i).decode("ascii") for i in imagens],
            "n_paginas": len(imagens)}


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
