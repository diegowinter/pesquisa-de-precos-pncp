"""
Extração de um documento inteiro: baixar → texto nativo por página → OCR nas escaneadas.

Este módulo é a implementação COMPARTILHADA da capacidade `pdf` (Fase 11, ADR-019). Roda nos
dois lados, sem bifurcação:

  - dentro do container, via `PdfEmProcessoAdapter`, quando `PDF_BASE_URL` está vazio;
  - dentro do `servidor_pdf.py`, na máquina que tem PyMuPDF (e, de preferência, a GPU do OCR).

Um módulo só, e não duas cópias, porque a lógica aqui é sutil de um jeito que não sobrevive a
ser reimplementada: o limiar de página escaneada, o DPI da rasterização e a regra de "uma
imagem por chamada de OCR" foram calibrados com dados reais. Duas versões divergindo dariam
textos diferentes conforme o serviço estivesse no ar ou não — e isso apareceria como variação
de qualidade de extração, não como erro.

O que ele NÃO faz: decidir estratégia (janela/completa/visão) ou chamar LLM. Isso continua na
etapa 5, que é dona da regra de negócio. Aqui é só "PDF vira texto".

ADR-012: o PDF é efêmero. Ele existe pelo tempo da extração, numa pasta temporária que o
`finally` remove — nem no adapter em processo, nem no servidor ele sobrevive à chamada.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable

from pesquisa_precos.core.coleta import consultar_arquivos
from pesquisa_precos.providers import ocr_pdf


def _hash_arquivos(pasta: str, nomes: list[str]) -> str:
    """sha1 do conteúdo concatenado — o `hash_arquivo` que permite detectar que o PNCP
    trocou o PDF de um documento já extraído (ADR-012 guarda o hash justamente para isso)."""
    h = hashlib.sha1()
    for nome in sorted(nomes):
        caminho = os.path.join(pasta, nome)
        try:
            with open(caminho, "rb") as f:
                for bloco in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(bloco)
        except OSError:
            continue
    return h.hexdigest()


def baixar(url_pncp: str, *, numero_controle: str, tipo_doc: str,
           numero_sequencial: str | None, numero_sequencial_ata: str | None,
           orgao_cnpj: str | None, ano: int | None, destino: str) -> list[str]:
    """Baixa os PDFs do documento em `destino`. Devolve os nomes dos arquivos.

    Prefere os identificadores internos (`listar_arquivos`), que é o caminho que a etapa 2
    preserva de propósito (ADR-012). `url_pncp` sozinha é a rota de exceção — ela aponta para a
    PÁGINA do documento no portal, não para o arquivo.
    """
    if all([tipo_doc, orgao_cnpj, ano, numero_sequencial]):
        arquivos = consultar_arquivos.listar_arquivos(
            tipo_doc, orgao_cnpj, ano, numero_sequencial, numero_sequencial_ata, silent=True)
        alvos = consultar_arquivos.selecionar_do_tipo(arquivos, tipo_doc)
        if alvos:
            return consultar_arquivos.baixar_arquivos(alvos, destino, silent=True)
    return []


def extrair_documento(
    url_pncp: str,
    *,
    numero_controle: str = "",
    tipo_doc: str = "",
    numero_sequencial: str | None = None,
    numero_sequencial_ata: str | None = None,
    orgao_cnpj: str | None = None,
    ano: int | None = None,
    ocr: Callable[[bytes], str] | None = None,
    pular_ocr: bool = False,
) -> dict:
    """PDF → `{paginas, n_paginas, n_ocr, hash, arquivos}`. O PDF não sobrevive à chamada.

    `ocr` é injetado (não importado) para que o servidor possa apontar para o seu OCR local e
    o adapter em processo para o `OCR_BASE_URL` do `.env`, sem que este módulo precise saber
    de configuração.

    Página escaneada é detectada pela DENSIDADE de texto nativo (< 100 chars), não pela
    ausência total: PDF escaneado costuma trazer um cabeçalho vetorial de meia dúzia de
    caracteres, e exigir zero deixaria essas páginas passarem sem OCR.
    """
    pasta = tempfile.mkdtemp(prefix="pdf_", dir=None)
    paginas: list[dict] = []
    n_ocr = 0
    try:
        nomes = baixar(url_pncp, numero_controle=numero_controle, tipo_doc=tipo_doc,
                       numero_sequencial=numero_sequencial,
                       numero_sequencial_ata=numero_sequencial_ata,
                       orgao_cnpj=orgao_cnpj, ano=ano, destino=pasta)
        if not nomes:
            return {"paginas": [], "n_paginas": 0, "n_ocr": 0, "hash": None, "arquivos": [],
                    "erro": "nenhum arquivo encontrado para o documento"}

        for nome in nomes:
            caminho = os.path.join(pasta, nome)
            try:
                extraidas = ocr_pdf.extrair_paginas(caminho)
            except Exception as exc:  # noqa: BLE001 — PDF corrompido não derruba o documento
                paginas.append({"arquivo": nome, "pagina": 0, "texto": "", "densidade": 0,
                                "escaneada": False, "fonte": "erro", "erro": str(exc)[:200]})
                continue
            for pg in extraidas:
                escaneada = ocr_pdf.pagina_escaneada(pg["densidade"])
                fonte_txt, texto = "nativo", pg["texto"]
                if escaneada and not pular_ocr and ocr is not None:
                    try:
                        # UMA imagem por chamada. Mandar o documento inteiro estoura o
                        # contexto do modelo de visão — é a regra crítica de `ocr_pdf`.
                        png = ocr_pdf.rasterizar(caminho, pg["_page_index"])
                        texto_ocr = ocr(png)
                        if texto_ocr:
                            fonte_txt, texto = "ocr", texto_ocr
                            n_ocr += 1
                    except Exception:  # noqa: BLE001 — sem OCR fica o nativo, não vazio
                        pass
                paginas.append({"arquivo": nome, "pagina": pg["pagina"], "texto": texto,
                                "densidade": pg["densidade"], "escaneada": escaneada,
                                "fonte": fonte_txt})
        return {"paginas": paginas, "n_paginas": len(paginas), "n_ocr": n_ocr,
                "hash": _hash_arquivos(pasta, nomes), "arquivos": nomes}
    finally:
        shutil.rmtree(pasta, ignore_errors=True)   # ADR-012: o PDF vive minutos


def rasterizar_documento(
    url_pncp: str,
    *,
    numero_controle: str = "",
    tipo_doc: str = "",
    numero_sequencial: str | None = None,
    numero_sequencial_ata: str | None = None,
    orgao_cnpj: str | None = None,
    ano: int | None = None,
    max_paginas: int | None = None,
) -> list[bytes]:
    """Páginas do documento como PNG — o que a estratégia `visao` consome (ADR-010).

    Separado de `extrair_documento` de propósito: a visão é ROTA DE EXCEÇÃO (documento
    suspeito/ilegível com muitos itens), e devolver imagens de 200 DPI em toda extração
    faria o caminho normal trafegar dezenas de MB por documento sem usar nada disso.

    `max_paginas` é aplicado ANTES de rasterizar, não depois: rasterizar 300 páginas para
    descartar 290 gastaria o tempo de CPU que o teto existe para evitar.
    """
    pasta = tempfile.mkdtemp(prefix="pdf_raster_")
    imagens: list[bytes] = []
    try:
        nomes = baixar(url_pncp, numero_controle=numero_controle, tipo_doc=tipo_doc,
                       numero_sequencial=numero_sequencial,
                       numero_sequencial_ata=numero_sequencial_ata,
                       orgao_cnpj=orgao_cnpj, ano=ano, destino=pasta)
        for nome in nomes:
            caminho = os.path.join(pasta, nome)
            try:
                paginas = ocr_pdf.extrair_paginas(caminho)
            except Exception:  # noqa: BLE001
                continue
            if max_paginas:
                paginas = paginas[: max(0, max_paginas - len(imagens))]
            for pg in paginas:
                try:
                    imagens.append(ocr_pdf.rasterizar(caminho, pg["_page_index"]))
                except Exception:  # noqa: BLE001
                    continue
            if max_paginas and len(imagens) >= max_paginas:
                break
        return imagens
    finally:
        shutil.rmtree(pasta, ignore_errors=True)   # ADR-012 vale aqui também
