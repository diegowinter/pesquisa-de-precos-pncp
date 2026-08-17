"""
Estratégia `visao` (Fase 8, ADR-010) — rasteriza cada página e envia a IMAGEM a um modelo de
visão, que devolve a tabela de itens "as it is" (não normaliza; cada documento tem um layout
diferente). NÃO é caminho principal: medição mostrou que custa mais que `janela`/`completa`
na distribuição real (uma chamada por página, ~15 páginas para extrair 2 itens no documento
típico). É rota de EXCEÇÃO para documento escaneado com tabela grande, onde o OCR corrido
embaralha colunas — acionamento sugerido em `etapas.e5_extrair`:
`doc_status ∈ {suspeito, ilegivel}` e `n_itens_sobreviventes` suficiente para amortizar o custo.

Portada de `etapas/e5_alt_a_tabela.py` (a extração por página) — a diferença em relação àquele
script é que aqui a tabela junta TODAS as páginas do documento (não só uma), para que o
casamento por item (`estrategias.base.casar_itens_contra_tabela`) tenha o documento inteiro
disponível, igual à `completa`.
"""

import glob
import os

from pesquisa_precos.providers import ocr_pdf


def extrair_tabela(curador, pasta_pdfs: str, max_paginas: int | None = None) -> list[dict]:
    """Rasteriza cada página dos PDFs em `pasta_pdfs` e extrai a tabela via visão (uma imagem
    por chamada — nunca o documento inteiro numa chamada só)."""
    tabela: list[dict] = []
    pdfs = sorted(glob.glob(os.path.join(pasta_pdfs, "*.pdf")) +
                 glob.glob(os.path.join(pasta_pdfs, "*.PDF")))
    for pdf in pdfs:
        try:
            paginas = ocr_pdf.extrair_paginas(pdf)
        except Exception:  # noqa: BLE001
            continue
        if max_paginas:
            paginas = paginas[:max_paginas]
        for pg in paginas:
            try:
                png = ocr_pdf.rasterizar(pdf, pg["_page_index"])
                tabela.extend(curador.extrair_tabela_pdf(png))
            except Exception:  # noqa: BLE001
                continue
    return tabela
