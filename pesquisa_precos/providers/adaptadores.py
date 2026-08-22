"""
Adapters concretos das capacidades (Fase 7) — cada um embrulha um cliente já validado atrás
dos `Protocol` de `protocolos.py`, sem reescrever a lógica de chamada.

**Todo adapter aqui é CLIENTE DE UM SERVIÇO.** Desde a ADR-021 não existe mais versão "em
processo": trabalho de GPU e de CPU intensiva (embedding, reranking, OCR, parse de PDF, BM25)
vive no repositório `pncp-servicos-locais` e é alcançado por HTTP. Este processo baixa, grava
no banco e conversa — nada mais. Era o mesmo problema do `--fonte csv`: dois caminhos para o
mesmo resultado, e a divergência entre eles não levantava exceção.

Nomes seguem docs/04_FASES.md §Fase 7:
  - `gpu_caseira`   → rerank remoto (serviço `gpu`)
  - `lm_studio`     → chat local (OpenAI-compatible)
  - `openrouter`    → chat pago (OpenAI-compatible)
  - `openai_compat` → chat genérico (qualquer servidor OpenAI-compatible além dos dois acima)

Retry/backoff: `Curador` já retria nativamente. O cliente de GPU (`gpu_remoto`) não tinha — o
adapter de rerank acrescenta um retry curto aqui, sem mexer em `gpu_remoto.py` (regra de "não
portar sem ler": preservar o cliente validado e só embrulhar).
"""

import time

import numpy as np

from pesquisa_precos.providers.protocolos import InfoProvedor

_RETRY_TENTATIVAS = 3
_RETRY_BACKOFF_S = 2.0


def _com_retry(fn, *args, **kwargs):
    ultimo_erro: Exception | None = None
    for tentativa in range(1, _RETRY_TENTATIVAS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — repassa a última após esgotar tentativas
            ultimo_erro = exc
            if tentativa < _RETRY_TENTATIVAS:
                time.sleep(_RETRY_BACKOFF_S * tentativa)
    assert ultimo_erro is not None
    raise ultimo_erro


class ChatAdapter:
    """`lm_studio` / `openrouter` / `openai_compat` — os três são o mesmo protocolo HTTP
    (OpenAI-compatible); o que muda é só base_url/modelo/chave, já resolvidos em `info`."""

    def __init__(self, info: InfoProvedor, *, api_key: str, curador_kwargs: dict | None = None):
        from pesquisa_precos.providers.llm_curador import Curador

        self.info = info
        kwargs = dict(curador_kwargs or {})
        # max_retries do próprio cliente OpenAI (honra Retry-After) — Curador já faz isso.
        # `setdefault` porque quem chama (ex.: etapa 3, concorrência alta) pode querer um
        # valor diferente sem colidir com o default daqui.
        kwargs.setdefault("max_retries", 6)
        self.curador = Curador(model=info.modelo, base_url=info.base_url, api_key=api_key,
                               **kwargs)

    def invocar(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage
        resp = self.curador.llm.invoke([HumanMessage(content=prompt)])
        return (resp.content or "").strip()

    def invocar_json(self, prompt: str) -> dict:
        return self.curador._invocar_json(prompt)  # noqa: SLF001 — mesmo pacote, uso interno

    def custo_estimado(self, tokens_in: int, tokens_out: int) -> float:
        if self.info.custo_in_por_mtok is None or self.info.custo_out_por_mtok is None:
            return 0.0
        return (tokens_in / 1_000_000) * self.info.custo_in_por_mtok + \
               (tokens_out / 1_000_000) * self.info.custo_out_por_mtok


class EmbedGpuCaseiraAdapter:
    """`gpu_caseira` (embed) — cliente HTTP do servidor de GPU (`gpu_remoto.EmbedderRemoto`),
    com retry curto por lote. FALLBACK PROIBIDO (ADR-006): se isto falhar após as tentativas,
    a exceção sobe e a etapa para — nunca cair para outro provedor de embedding."""

    def __init__(self, info: InfoProvedor, *, api_key: str, cache_path: str | None = None):
        from pesquisa_precos.providers.gpu_remoto import EmbedderRemoto

        self.info = info
        self._cliente = EmbedderRemoto(info.base_url, api_key, cache_path=cache_path)

    def embed_textos(self, textos: list[str]) -> np.ndarray:
        return _com_retry(self._cliente.embed_textos, textos)

    def salvar_cache(self) -> None:
        self._cliente.salvar_cache()

    def liberar(self) -> None:
        self._cliente.liberar()


class RerankGpuCaseiraAdapter:
    """`gpu_caseira` (rerank) — cliente HTTP do servidor de GPU (`gpu_remoto.RerankerRemoto`),
    com retry curto por lote. Fallback é PERMITIDO em `rerank` (ADR-006) — quem decide se usa
    é `resolver.py`/a etapa, não este adapter."""

    def __init__(self, info: InfoProvedor, *, api_key: str):
        from pesquisa_precos.providers.gpu_remoto import RerankerRemoto

        self.info = info
        self._cliente = RerankerRemoto(info.base_url, api_key, batch=info.batch_size)

    def score_pares(self, pares: list[tuple[str, str]]) -> np.ndarray:
        return _com_retry(self._cliente.score_pares, pares)

    def liberar(self) -> None:
        self._cliente.liberar()


class PdfRemotoAdapter:
    """`pdf` — este processo baixa os arquivos do PNCP e manda os bytes para o serviço, que
    extrai o texto, rasteriza as páginas escaneadas e chama o OCR por dentro. Volta só texto.

    É o que tira `pymupdf` e a rasterização a 200 DPI daqui, sem trazer o conhecimento da API
    do PNCP para lá (ADR-021).
    """

    def __init__(self, info: InfoProvedor, *, api_key: str, timeout_s: int = 600):
        self.info = info
        self._api_key = api_key
        self._timeout = timeout_s

    def extrair(self, url_pncp: str, *, numero_controle: str = "", tipo_doc: str = "",
                numero_sequencial: str | None = None, numero_sequencial_ata: str | None = None,
                orgao_cnpj: str | None = None, ano: int | None = None) -> dict:
        return self._enviar("/extrair", url_pncp, numero_controle=numero_controle,
                            tipo_doc=tipo_doc, numero_sequencial=numero_sequencial,
                            numero_sequencial_ata=numero_sequencial_ata,
                            orgao_cnpj=orgao_cnpj, ano=ano)

    def rasterizar(self, url_pncp: str, *, max_paginas: int | None = None, **ids) -> list[bytes]:
        import base64

        resp = self._enviar("/rasterizar", url_pncp, campos={"max_paginas": max_paginas}, **ids)
        # base64 e não multipart na volta: são poucas páginas (a visão tem teto) e manter uma
        # resposta JSON simplifica o servidor, que já fala JSON em tudo.
        return [base64.b64decode(b) for b in resp.get("paginas_png", [])]

    # ── download + upload ────────────────────────────────────────────────────────────────
    # Baixar é I/O barato, e o cliente da API do PNCP já vive aqui (é o mesmo da etapa 2).
    # Duplicá-lo do outro lado daria duas implementações da mesma API pública para manter em
    # sincronia sem tirar carga nenhuma deste processo. O que vai para o serviço é o trabalho
    # caro: parse com PyMuPDF, rasterização a 200 DPI e OCR na GPU (ADR-021).

    def _baixar(self, url_pncp: str, destino: str, *, tipo_doc: str,
                numero_sequencial: str | None, numero_sequencial_ata: str | None,
                orgao_cnpj: str | None, ano: int | None) -> list[str]:
        from pesquisa_precos.core.coleta import consultar_arquivos

        if not all([tipo_doc, orgao_cnpj, ano, numero_sequencial]):
            return []
        arquivos = consultar_arquivos.listar_arquivos(
            tipo_doc, orgao_cnpj, ano, numero_sequencial, numero_sequencial_ata, silent=True)
        alvos = consultar_arquivos.selecionar_do_tipo(arquivos, tipo_doc)
        return consultar_arquivos.baixar_arquivos(alvos, destino, silent=True) if alvos else []

    def _enviar(self, rota: str, url_pncp: str, *, campos: dict | None = None, **ids) -> dict:
        import os
        import shutil
        import tempfile

        import requests

        pasta = tempfile.mkdtemp(prefix="pdf_envio_")
        try:
            nomes = self._baixar(
                url_pncp, pasta,
                tipo_doc=ids.get("tipo_doc") or "",
                numero_sequencial=ids.get("numero_sequencial"),
                numero_sequencial_ata=ids.get("numero_sequencial_ata"),
                orgao_cnpj=ids.get("orgao_cnpj"), ano=ids.get("ano"))
            if not nomes:
                return {"paginas": [], "n_paginas": 0, "n_ocr": 0, "hash": None, "arquivos": [],
                        "erro": "nenhum arquivo encontrado para o documento"}
            abertos = [open(os.path.join(pasta, n), "rb") for n in nomes]
            try:
                # Timeout generoso: um documento de 300 páginas com OCR leva minutos. Curto
                # demais transformaria trabalho de GPU já feito em erro de rede.
                resp = requests.post(
                    f"{self.info.base_url.rstrip('/')}{rota}",
                    files=[("arquivos", (n, f, "application/pdf"))
                           for n, f in zip(nomes, abertos)],
                    data={k: str(v) for k, v in (campos or {}).items() if v is not None},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=(30, self._timeout),
                )
            finally:
                for f in abertos:
                    f.close()
            resp.raise_for_status()
            return resp.json()
        finally:
            # ADR-012: o PDF é efêmero dos DOIS lados. Aqui ele vive o tempo do upload.
            shutil.rmtree(pasta, ignore_errors=True)


class PareamentoRemotoAdapter:
    """`pareamento` remoto — BM25 + cosseno + corte, do outro lado de um HTTP."""

    def __init__(self, info: InfoProvedor, *, api_key: str, timeout_s: int = 1800):
        self.info = info
        self._api_key = api_key
        self._timeout = timeout_s

    def parear(self, catalogo: list[dict], itens: list[dict], *,
               piso: float, top_k: int | None = None) -> list[dict]:
        import requests

        resp = requests.post(
            f"{self.info.base_url.rstrip('/')}/parear",
            json={"catalogo": catalogo, "itens": itens, "piso": piso, "top_k": top_k},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=(30, self._timeout),
        )
        resp.raise_for_status()
        return resp.json()["pares"]


def custo_estimado_generico(info: InfoProvedor, tokens_in: int, tokens_out: int) -> float:
    """Mesma fórmula de `ChatAdapter.custo_estimado`, exposta solta p/ `estimar()` de etapas
    que não têm (e não precisam) instanciar o adapter de verdade (docs/03_ETAPAS.md §1.1
    regra 5: `estimar()` nunca gasta nem chama provedor pago)."""
    if info.custo_in_por_mtok is None or info.custo_out_por_mtok is None:
        return 0.0
    return (tokens_in / 1_000_000) * info.custo_in_por_mtok +            (tokens_out / 1_000_000) * info.custo_out_por_mtok
