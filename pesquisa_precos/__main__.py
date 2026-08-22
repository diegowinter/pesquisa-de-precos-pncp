"""
`python -m pesquisa_precos` — o único jeito de subir o sistema (Fase 13, ADR-020).

Um processo, uma porta: HTML e JSON saem da mesma app (`web/app.py`), que monta os routers
de `api/routers/` sob `/api`. Antes da Fase 13 eram dois entrypoints (`-m pesquisa_precos.web`
na 8001 e `-m pesquisa_precos.api` na 8000) servindo os mesmos `services/`.

`reload` fica DESLIGADO por padrão de propósito: este é o processo que sobe subprocesso de
etapa (ADR-002), e recarregar no meio de uma etapa rodando seria surpreendente. Ligue com
`WEB_RELOAD=1` só em desenvolvimento.
"""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "pesquisa_precos.web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8001")),
        reload=bool(os.getenv("WEB_RELOAD")),
    )


if __name__ == "__main__":
    main()
