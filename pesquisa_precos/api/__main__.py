"""Sobe a API: `python -m pesquisa_precos.api`. `API_HOST`/`API_PORT` no `.env` para além do
default local; `uvicorn pesquisa_precos.api.app:app --reload` direto serve para desenvolvimento
com hot-reload, que este entrypoint não liga por padrão (é o mesmo processo que dispara
subprocesso de etapa — reload no meio de uma etapa rodando seria surpreendente)."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "pesquisa_precos.api.app:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
