"""
Etapa 5b — Extração guiada por item a partir do texto (5a) + destino (paralelo, resumível).

Lê o texto já parseado/OCR'd pela 5a (data/5_pdf_texto.csv) e, para cada item sobrevivente,
pede ao LLM só AQUELE item (janela multi-âncora: descrição + preço) — descrição rica + preço +
quantidade. CONFIRMA o item pela QUANTIDADE (ou por preço exato alto) e então CAPTURA o preço do
PDF como valor real: a API traz o valor estimado, o PDF o homologado/registrado. Grava os dois
(preco_api, preco_pdf) e a divergência — preço diferente é sinalizado, NÃO descartado.
Grava data/5_itens_enriquecidos.csv (resumível por item_key) e, ao fim, consolida o destino por
item em data/5_itens_destino.csv (detector de PDF-trocado):
  - manter    → item enriquecido (pdf_ok / pdf_ok_diverge / pdf_ok_sem_preco);
  - revisar   → item em documento suspeito (nada confirmou → PDF trocado) ou ilegível;
  - descartar → falha isolada em documento saudável (item não confirmado por qtd/preço).

Entradas: data/4_itens_sobreviventes.csv, data/5_pdf_texto.csv (rode a 5a antes).
Saídas: data/5_itens_enriquecidos.csv, data/5_itens_destino.csv. Erros: erros/5_erros.csv.
Chave de resumo: item_key.

NÃO fazer: usar o preço como critério de aceite do item (é SAÍDA, não filtro), ancorar a
janela só na descrição (perde a cláusula de preço em contrato grande) ou descartar item por
divergência de preço.

Uso: python -m pesquisa_precos.etapas.e5b_extrair [--provedor openrouter|local]
                                                  [--concurrency 8] [--limite N]
"""

import csv
import re
import sys
import threading
import unicodedata
from collections import defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from pydantic import BaseModel, Field

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import custo_por_chamada, exigir
from pesquisa_precos.core.io_seguro import (
    EscritorSeguro,
    escrever_csv,
    ler_chaves_concluidas,
    ler_csv,
)
from pesquisa_precos.core.paralelo import executar_paralelo
from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.db import sessao as db
from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa
from pesquisa_precos.providers.llm_curador import Curador

CHAVE = "5b"
VERSAO_CODIGO = "1.0.0"

SOBREVIVENTES = paths.E4_SOBREVIVENTES
TEXTO = paths.E5_PDF_TEXTO
SAIDA = paths.E5_ENRIQUECIDOS
DESTINO = paths.E5_DESTINO
ERROS = paths.ERROS_5

COLS = ["item_key", "descricao_final", "fonte_descricao", "preco_api", "preco_pdf",
        "divergencia_preco", "paginas_ocr", "enriquecimento"]
DESTINO_COLS = ["item_key", "enriquecimento", "doc_status", "destino"]
JANELA = 3000          # raio ao redor da âncora de descrição
RAIO_PRECO = 1500      # raio ao redor de cada ocorrência do preço
MAX_JANELA = 9000      # teto de chars da janela final (soma dos trechos)


class Params(BaseModel):
    provedor: str = Field("openrouter", description="Provedor de LLM [local|openrouter]")
    concurrency: int = Field(8, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    limite: int | None = Field(None, description="Teto de itens a extrair (debug)")


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _variantes_preco(v) -> list[str]:
    """Formatos BR do preço p/ localizar a cláusula/linha do valor (ex.: 578538.24 →
    '578.538,24' e '578538,24'). É o mesmo fingerprint que a âncora usa pra validar."""
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return []
    if f <= 0:
        return []
    intp, dec = f"{f:.2f}".split(".")
    intp_pt = re.sub(r"(?<=\d)(?=(\d{3})+$)", ".", intp)
    return list({f"{intp},{dec}", f"{intp_pt},{dec}"})


def _merge_intervalos(ints: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ints):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def janela_para_item(texto_doc: str, item: dict) -> str:
    """Recorta o texto ao redor de MÚLTIPLAS âncoras: a descrição do item E cada ocorrência
    do preço. Em contratos grandes a descrição (objeto) e o valor (cláusula de preço) ficam
    em seções distantes; ancorar só na descrição perdia o preço e derrubava a validação."""
    alvo = _norm(texto_doc)
    ancoras: list[tuple[int, int]] = []  # (centro, raio)

    frag = _norm(str(item.get("descricao_api", "")))[:40]
    pos = alvo.find(frag) if frag else -1
    if pos < 0 and item.get("numeroItem"):
        pos = alvo.find(f"item {str(item['numeroItem']).strip()}")
    if pos >= 0:
        ancoras.append((pos, JANELA))

    for var in _variantes_preco(item.get("preco_unitario", "")):
        start, achadas = 0, 0
        while achadas < 2:  # até 2 ocorrências por variante (limita custo/ruído)
            p = texto_doc.find(var, start)
            if p < 0:
                break
            ancoras.append((p, RAIO_PRECO))
            start, achadas = p + len(var), achadas + 1

    if not ancoras:
        return texto_doc[: JANELA * 4]

    intervalos = _merge_intervalos(
        [(max(0, c - r), min(len(texto_doc), c + r)) for c, r in ancoras])
    partes, total = [], 0
    for a, b in intervalos:
        if total >= MAX_JANELA:
            break
        b = min(b, a + (MAX_JANELA - total))
        partes.append(texto_doc[a:b])
        total += b - a
    return "\n[...]\n".join(partes)


# Match exato de preço (até os centavos) acima deste valor já é fingerprint único: confirma
# o item mesmo sem casar a quantidade (contratos de serviço têm qtd=1 e o doc não a reafirma).
PRECO_FINGERPRINT = 1000.0


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def validar_extracao(extraido: dict, item: dict) -> tuple[str, float | None, float | None]:
    """Confirma que o LLM leu o item CERTO e devolve (motivo, preco_pdf, divergencia).

    O item é confirmado pela QUANTIDADE (fingerprint anti-colisão/PDF-trocado) OU por um
    match exato de preço alto. Confirmado o item, o PREÇO deixa de ser critério de aceite e
    vira SAÍDA: a API traz o valor estimado, o PDF o valor homologado/registrado (o que
    interessa). Divergência é sinalizada, não descartada.
      - pdf_ok               → item confirmado e preço do PDF ≈ preço da API;
      - pdf_ok_diverge       → item confirmado, preço do PDF ≠ API (estimado vs. registrado);
      - pdf_ok_preco_suspeito→ item confirmado, mas o preço extraído é implausível vs. a API
                               (provável misparse de número BR, ex.: 107.222,00 → 107,22);
      - pdf_ok_sem_preco     → item confirmado (descrição rica), sem preço legível no PDF;
      - qtd_nao_confere      → achou algo, mas nem quantidade nem preço confirmam (não enriquece);
      - nao_encontrado       → LLM não localizou o item no texto.
    """
    if not (extraido.get("encontrado") and extraido.get("descricao_completa")):
        return "nao_encontrado", None, None
    pe, pa = _num(extraido.get("preco_unitario")), _num(item.get("preco_unitario"))
    qe, qa = _num(extraido.get("quantidade")), _num(item.get("quantidade"))
    qtd_ok = qe is not None and qa is not None and abs(qe - qa) <= max(1.0, 0.01 * abs(qa))
    preco_ok = pe is not None and pa is not None and pa != 0 and abs(pe - pa) / abs(pa) <= 0.01
    confirmado = qtd_ok or (preco_ok and pa is not None and abs(pa) >= PRECO_FINGERPRINT)
    if not confirmado:
        return "qtd_nao_confere", None, None
    if pe is None or pe <= 0:
        return "pdf_ok_sem_preco", None, None
    if preco_ok:
        return "pdf_ok", pe, 0.0
    div = round((pe - pa) / pa, 4) if pa else None
    # Banda de sanidade: homologado costuma ficar em [0,3× … 3×] do estimado. Fora disso é quase
    # sempre misparse de milhar/decimal (ou item errado) — mantém a descrição, mas marca o preço.
    if pa and (pe < 0.3 * abs(pa) or pe > 3.0 * abs(pa)):
        return "pdf_ok_preco_suspeito", pe, div
    return "pdf_ok_diverge", pe, div


def carregar_texto_docs() -> tuple[dict, dict]:
    """Lê 5_pdf_texto.csv (streaming) → (texto_doc concatenado por página, nº páginas OCR/doc)."""
    if not TEXTO.exists():
        raise SystemExit(f"{TEXTO} ausente. Rode a etapa 5a (OCR/parse) antes.")
    paginas = defaultdict(list)          # doc_key -> [(pagina:int, texto)]
    ocr_por_doc = defaultdict(set)       # doc_key -> {paginas via ocr}
    with open(TEXTO, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            dk = r["doc_key"]
            try:
                pg = int(r["pagina"])
            except (TypeError, ValueError):
                pg = 0
            paginas[dk].append((pg, r.get("texto", "")))
            if r.get("fonte") == "ocr":
                ocr_por_doc[dk].add(pg)
    texto_doc = {dk: "\n".join(t for _, t in sorted(ps)) for dk, ps in paginas.items()}
    n_ocr = {dk: len(s) for dk, s in ocr_por_doc.items()}
    return texto_doc, n_ocr


def consolidar_destino(saida_csv: str, sob: pd.DataFrame, dest_csv: str) -> dict:
    """Deriva doc_status (detector de PDF-trocado) + destino por item, dos motivos da extração."""
    motivos = {r["item_key"]: r.get("enriquecimento", "") for r in ler_csv(saida_csv)}
    item_doc = dict(zip(sob["item_key"], sob.get("pasta_arquivos", pd.Series([""] * len(sob)))))

    por_doc: dict[str, list[str]] = defaultdict(list)
    for ik, m in motivos.items():
        por_doc[item_doc.get(ik, "")].append(m)
    doc_status: dict[str, str] = {}
    for d, ms in por_doc.items():
        n_ok = sum(m.startswith("pdf_ok") for m in ms)
        n_legivel = sum(m != "sem_texto" for m in ms)
        doc_status[d] = "ilegivel" if n_legivel == 0 else ("suspeito" if n_ok == 0 else "ok")

    linhas, contagem = [], defaultdict(int)
    for ik, m in motivos.items():
        st = doc_status.get(item_doc.get(ik, ""), "ok")
        destino = "manter" if m.startswith("pdf_ok") else ("revisar" if st in ("suspeito", "ilegivel") else "descartar")
        contagem[destino] += 1
        linhas.append({"item_key": ik, "enriquecimento": m, "doc_status": st, "destino": destino})
    escrever_csv(dest_csv, DESTINO_COLS, linhas)
    contagem["docs_suspeitos"] = sum(1 for v in doc_status.values() if v == "suspeito")
    contagem["docs_ilegiveis"] = sum(1 for v in doc_status.values() if v == "ilegivel")
    contagem["preco_diverge"] = sum(m == "pdf_ok_diverge" for m in motivos.values())
    contagem["preco_suspeito"] = sum(m == "pdf_ok_preco_suspeito" for m in motivos.values())
    contagem["sem_preco"] = sum(m == "pdf_ok_sem_preco" for m in motivos.values())
    return contagem


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Uma chamada de LLM por ITEM sobrevivente ainda não enriquecido (não há dedup aqui)."""
    if not SOBREVIVENTES.exists():
        return Estimativa(detalhes={"aviso": f"{SOBREVIVENTES} ausente — rode a etapa 4 antes."})
    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [k for k in sob["item_key"] if k not in feitas]
    n = len(pend) if not params.limite else min(len(pend), params.limite)
    preco = custo_por_chamada(ctx.config, params.provedor)
    return Estimativa(
        unidades=n, chamadas_llm=n,
        custo_usd=None if preco is None else n * preco,
        duracao_s=n / max(params.concurrency, 1) * 2,
        detalhes={"itens_sobreviventes": len(sob), "já_enriquecidos": len(feitas),
                  "texto_disponivel": TEXTO.exists()},
    )


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    cfg = ctx.config
    msg = exigir(cfg, params.provedor)
    if msg:
        raise SystemExit(msg)
    if not SOBREVIVENTES.exists():
        raise SystemExit(f"{SOBREVIVENTES} ausente. Rode a etapa 4 antes.")

    sob = pd.read_csv(SOBREVIVENTES, dtype=str, encoding="utf-8").fillna("")
    texto_doc, n_ocr_doc = carregar_texto_docs()

    feitas = ler_chaves_concluidas(str(SAIDA), "item_key")
    pend = [r.to_dict() for _, r in sob.iterrows() if r["item_key"] not in feitas]
    if params.limite:
        pend = pend[: params.limite]
    if not pend:
        ctx.log("info", "[5b] Nada a extrair (tudo já feito). Consolidando destino...")
        cont = consolidar_destino(str(SAIDA), sob, str(DESTINO))
        ctx.log("info", f"[5b] Destino: manter={cont['manter']} descartar={cont['descartar']} "
                        f"revisar={cont['revisar']} (preço diverge={cont['preco_diverge']}, "
                        f"sem preço={cont['sem_preco']})")
        return ResultadoEtapa(metricas=dict(cont))

    ctx.log("info", f"[bold][5b] {len(pend)} itens a extrair[/] (já feitos: {len(feitas)}), "
                    f"provedor: {params.provedor}, concorrência: {params.concurrency}")

    # Prompt 'extrair_item_pdf' resolvido UMA vez, fora dos workers (Fase 6, ver etapa 3).
    try:
        with db.sessao() as sessao:
            prompts_ativos = prompts_resolver.carregar_ativos(sessao, ["extrair_item_pdf"])
    except Exception:  # noqa: BLE001 — sem banco configurado, cai no prompt hardcoded
        prompts_ativos = {}

    # Um Curador por thread (compartilhar um cliente serializa as chamadas — ver etapa 3).
    _tls = threading.local()

    def _curador():
        if not hasattr(_tls, "c"):
            _tls.c = Curador.from_provedor(cfg, params.provedor, max_retries=6,
                                           prompts_ativos=prompts_ativos)
        return _tls.c

    def _linha(item, res):
        pp = res.get("preco_pdf")
        dv = res.get("divergencia")
        return {"item_key": item["item_key"], "descricao_final": res["descricao_final"],
                "fonte_descricao": res["fonte"],
                "preco_api": "" if res.get("preco_api") is None else res["preco_api"],
                "preco_pdf": "" if pp is None else pp,
                "divergencia_preco": "" if dv is None else dv,
                "paginas_ocr": res["n_ocr"], "enriquecimento": res["motivo"]}

    n_erros = [0]
    with EscritorSeguro(str(SAIDA), COLS) as w:
        def fn(item):
            pasta = item.get("pasta_arquivos", "")
            texto = texto_doc.get(pasta, "")
            base = {"descricao_final": item.get("descricao_api", ""), "fonte": "api",
                    "preco_api": _num(item.get("preco_unitario")), "preco_pdf": None,
                    "divergencia": None, "n_ocr": int(n_ocr_doc.get(pasta, 0))}
            if not texto.strip():
                return {**base, "motivo": "sem_texto"}
            ext = _curador().extrair_item_pdf(janela_para_item(texto, item), item)
            motivo, preco_pdf, div = validar_extracao(ext, item)
            if motivo.startswith("pdf_ok"):
                return {**base, "descricao_final": ext["descricao_completa"], "fonte": "pdf",
                        "preco_pdf": preco_pdf, "divergencia": div, "motivo": motivo}
            return {**base, "motivo": motivo}

        def ok(item, res):
            w.escrever(_linha(item, res))

        def err(item, exc):
            n_erros[0] += 1
            ctx.erro_item(item["item_key"], exc)
            w.escrever(_linha(item, {
                "descricao_final": item.get("descricao_api", ""), "fonte": "api",
                "preco_api": _num(item.get("preco_unitario")), "preco_pdf": None,
                "divergencia": None, "n_ocr": int(n_ocr_doc.get(item.get("pasta_arquivos", ""), 0)),
                "motivo": "erro"}))

        ctx.progresso(0, len(pend), descricao="extraindo")
        executar_paralelo(pend, fn, concurrency=params.concurrency, on_result=ok, on_error=err,
                          on_progress=lambda f, t: ctx.progresso(f, t))

    cont = consolidar_destino(str(SAIDA), sob, str(DESTINO))
    ctx.log("info", f"[bold green][5b] Concluído.[/] → {SAIDA}")
    ctx.log("info", f"[5b] Destino: manter={cont['manter']} descartar={cont['descartar']} "
                    f"revisar={cont['revisar']} (docs suspeitos={cont['docs_suspeitos']}, "
                    f"ilegíveis={cont['docs_ilegiveis']}) → {DESTINO}")
    ctx.log("info", f"[5b] Preço: {cont['preco_diverge']} divergentes (estimado vs. registrado), "
                    f"{cont['preco_suspeito']} suspeitos (provável misparse), "
                    f"{cont['sem_preco']} sem preço legível.")

    return ResultadoEtapa(
        processados=len(pend) - n_erros[0], erros=n_erros[0],
        metricas=dict(cont),
    )


def main() -> None:
    from pesquisa_precos.cli.app import rodar_etapa_isolada

    rodar_etapa_isolada(CHAVE)


if __name__ == "__main__":
    main()
