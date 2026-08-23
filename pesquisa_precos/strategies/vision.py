"""
Estratégia `visao` (Fase 8, ADR-010) — rasteriza cada página e envia a IMAGEM a um modelo de
visão, que devolve a tabela de itens "as it is" (não normaliza; cada documento tem um layout
diferente). NÃO é caminho principal: medição mostrou que custa mais que `janela`/`completa`
na distribuição real (uma chamada por página, ~15 páginas para extrair 2 itens no documento
típico). É rota de EXCEÇÃO para documento escaneado com tabela grande, onde o OCR corrido
embaralha colunas — acionamento sugerido em `steps.e5_extract`:
`doc_status ∈ {suspeito, ilegivel}` e `n_itens_sobreviventes` suficiente para amortizar o custo.

Portada de `etapas/e5_alt_a_tabela.py` (a extração por página) — a diferença em relação àquele
script é que aqui a tabela junta TODAS as páginas do documento (não só uma), para que o
casamento por item (`strategies.base.casar_itens_contra_tabela`) tenha o documento inteiro
disponível, igual à `completa`.
"""

from collections.abc import Sequence


def extrair_tabela(curador, imagens: Sequence[bytes], max_paginas: int | None = None) -> list[dict]:
    """Extrai a tabela via visão a partir das PÁGINAS JÁ RASTERIZADAS (uma imagem por chamada
    — nunca o documento inteiro numa chamada só).

    Fase 11 (ADR-019): antes esta função recebia a PASTA dos PDFs e rasterizava ela mesma, com
    PyMuPDF. Rasterizar é justamente o que sai do container junto com o parse — quem entrega as
    imagens agora é a capacidade `pdf` (local ou remota). A regra de negócio (uma imagem por
    chamada, teto de páginas) não mudou de lugar: continua aqui.
    """
    tabela: list[dict] = []
    if max_paginas:
        imagens = imagens[:max_paginas]
    for png in imagens:
        try:
            tabela.extend(curador.extrair_tabela_pdf(png))
        except Exception:  # noqa: BLE001 — página ruim não derruba o documento
            continue
    return tabela
