"""
Etapa 5a — Parse + OCR dos PDFs dos sobreviventes (paralelo, resumível por documento).

Isola o gargalo de OCR da extração LLM (5b). Para cada documento (pasta_arquivos) dos itens
sobreviventes: abre os PDFs com PyMuPDF, extrai o texto nativo por página; página escaneada
(< 100 chars nativos) é rasterizada e enviada ao servidor de OCR remoto (OCR_BASE_URL — o
PaddleOCR/GPU). O texto de cada página é gravado em data/5_pdf_texto.csv (append crash-safe,
fsync por linha), resumível por doc_key: relançar pula os documentos já processados.

A concorrência (--concurrency) processa vários DOCUMENTOS ao mesmo tempo; cada worker faz um
documento por vez (páginas em sequência). Case este valor com o --workers do servidor de OCR.

Entrada: data/4_itens_sobreviventes.csv (coluna pasta_arquivos).
Saída: data/5_pdf_texto.csv (doc_key, arquivo, pagina, fonte, texto). Chave de resumo: doc_key.

NÃO fazer: mandar o documento inteiro ao OCR — é uma imagem de página por chamada.

Uso: python -m pesquisa_precos.etapas.e5a_ocr [--concurrency 3] [--pular-ocr] [--limite-docs N]
"""

import glob
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import exigir
from pesquisa_precos.core.io_seguro import EscritorSeguro, ler_chaves_concluidas
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers import ocr_pdf

CHAVE = "5a"
VERSAO_CODIGO = "1.0.0"

SOBREVIVENTES = paths.E4_SOBREVIVENTES
SAIDA = paths.E5_PDF_TEXTO

COLS = ["doc_key", "arquivo", "pagina", "fonte", "texto"]


class Params(BaseModel):
    concurrency: int = Field(
        3, ge=1, le=32,
        description="Documentos processados em paralelo (case com --workers do servidor OCR)")
    pular_ocr: bool = Field(False, description="Só PDFs nativos (não chama o OCR)")
    limite_docs: int | None = Field(None, description="Teto de documentos (debug)")


def parse_documento(pasta: str, pular_ocr: bool, provedor_ocr) -> list[dict]:
    """Devolve [{doc_key, arquivo, pagina, fonte, texto}] para todos os PDFs da pasta.
    Página escaneada vai ao OCR (uma imagem por chamada — nunca o doc inteiro). `provedor_ocr`
    satisfaz `providers.protocolos.ProvedorOcr` (Fase 7: banco → `.env`, ver `ctx.provedores`).
    """
    registros = []
    for pdf in sorted(glob.glob(os.path.join(pasta, "*.pdf")) + glob.glob(os.path.join(pasta, "*.PDF"))):
        try:
            paginas = ocr_pdf.extrair_paginas(pdf)
        except Exception:  # noqa: BLE001
            continue
        for pg in paginas:
            fonte, texto = "nativo", pg["texto"]
            if ocr_pdf.pagina_escaneada(pg["densidade"]) and not pular_ocr:
                try:
                    png = ocr_pdf.rasterizar(pdf, pg["_page_index"])
                    ocr_txt = provedor_ocr.ocr_pagina(png)
                    if ocr_txt:
                        fonte, texto = "ocr", ocr_txt
                except Exception:  # noqa: BLE001
                    pass
            registros.append({"doc_key": pasta, "arquivo": os.path.basename(pdf),
                              "pagina": pg["pagina"], "fonte": fonte, "texto": texto})
    return registros


def _documentos_pendentes(params: Params) -> tuple[list[str], list[str], set]:
    """(todos os documentos, os que faltam, os já feitos)."""
    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    docs = [p for p in dict.fromkeys(sob["pasta_arquivos"]) if p and os.path.isdir(p)]
    feitos = ler_chaves_concluidas(str(SAIDA), "doc_key")
    pend = [d for d in docs if d not in feitos]
    if params.limite_docs:
        pend = pend[: params.limite_docs]
    return docs, pend, feitos


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Documentos a parsear. Custo é CPU/GPU (OCR), não token — `custo_usd` fica em zero."""
    if not SOBREVIVENTES.exists():
        return Estimativa(detalhes={"aviso": f"{SOBREVIVENTES} ausente — rode a etapa 4 antes."})
    docs, pend, feitos = _documentos_pendentes(params)
    return Estimativa(
        unidades=len(pend), chamadas_llm=0, custo_usd=0.0,
        detalhes={"documentos_visiveis": len(docs), "já_processados": len(feitos),
                  "ocr": "desligado" if params.pular_ocr else "ligado"},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    cfg = ctx.config
    if not params.pular_ocr:
        # A validação legada de `.env` só se aplica quando a resolução caiu no `.env`
        # (Fase 7 — banco configurado já traz tudo que precisa, ver e3_classificar).
        if ctx.provedores.resolucao("ocr").origem == "env":
            msg = exigir(cfg, "ocr")
            if msg:
                raise SystemExit(msg)
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    docs, pend, feitos = _documentos_pendentes(params)
    if not pend:
        ctx.log("info", "[5a] Nada a fazer (todos os documentos já processados).")
        return ResultadoEtapa(metricas={"documentos_ja_processados": len(feitos)})

    ctx.log("info", f"[bold][5a] {len(docs)} documentos, já feitos: {len(feitos)}, "
                    f"pendentes: {len(pend)}[/] — concorrência: {params.concurrency}"
                    f"{' (SEM OCR)' if params.pular_ocr else ''}")

    # Fase 7 (ADR-006): provedor de OCR único por execução, reaproveitado entre documentos
    # (o adapter é sem estado por chamada — diferente do chat, não precisa de um por thread).
    provedor_ocr = None if params.pular_ocr else ctx.provedores.ocr

    n_ocr = [0]
    n_erros = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        def fn(pasta):
            return parse_documento(pasta, params.pular_ocr, provedor_ocr)

        def ok(_pasta, regs):
            for r in regs:
                if r["fonte"] == "ocr":
                    n_ocr[0] += 1
                w.escrever(r)

        def err(pasta, exc):
            n_erros[0] += 1
            ctx.log("aviso", f"[yellow][5a] erro em {os.path.basename(pasta)}: "
                             f"{str(exc)[:120]}[/]")

        ctx.progresso(0, len(pend), descricao="ocr/parse")
        executar_paralelo(pend, fn, concurrency=params.concurrency, on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    ctx.log("info", f"[bold green][5a] Concluído.[/] {n_ocr[0]} páginas via OCR. → {SAIDA}")

    return ResultadoEtapa(
        processados=len(pend) - n_erros[0], erros=n_erros[0],
        metricas={"documentos": len(pend), "paginas_via_ocr": n_ocr[0]},
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
