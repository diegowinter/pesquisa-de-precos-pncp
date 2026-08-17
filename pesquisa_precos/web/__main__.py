"""`python -m pesquisa_precos.web` — sobe a interface web (Fase 5) em uvicorn."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("pesquisa_precos.web.app:app", host="0.0.0.0",
               port=int(os.getenv("WEB_PORT", "8001")), reload=bool(os.getenv("WEB_RELOAD")))
