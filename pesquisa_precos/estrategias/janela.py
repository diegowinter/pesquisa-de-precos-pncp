"""
Estratégia `janela` (Fase 8, ADR-010) — recorte multi-âncora ao redor da descrição do item E
de cada ocorrência do preço, uma chamada de LLM por item. Ganha na mediana (2 itens/doc).

Portada de `etapas/e5b_extrair.py` (v3 anterior à Fase 8) SEM mudar a lógica de recorte nem de
validação — só o transporte (agora devolve os campos para `etapas.e5_extrair` gravar, em vez
de escrever CSV direto).

Por que multi-âncora: em contratos grandes a descrição (objeto) e o valor (cláusula de preço)
ficam em seções distantes do documento. Ancorar só na descrição perdia o preço e derrubava a
validação — isso já foi corrigido, não regredir (docs/08_CONVENCOES.md §4).
"""

import re
import unicodedata

JANELA = 3000          # raio ao redor da âncora de descrição
RAIO_PRECO = 1500      # raio ao redor de cada ocorrência do preço
MAX_JANELA = 9000       # teto de chars da janela final (soma dos trechos)


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


def janela_para_item(texto_doc: str, item: dict, janela_max: int = MAX_JANELA,
                     raio_desc: int = JANELA, raio_preco: int = RAIO_PRECO) -> str:
    """Recorta o texto ao redor de MÚLTIPLAS âncoras: a descrição do item E cada ocorrência
    do preço."""
    alvo = _norm(texto_doc)
    ancoras: list[tuple[int, int]] = []  # (centro, raio)

    frag = _norm(str(item.get("descricao_api", "")))[:40]
    pos = alvo.find(frag) if frag else -1
    if pos < 0 and item.get("numeroItem"):
        pos = alvo.find(f"item {str(item['numeroItem']).strip()}")
    if pos >= 0:
        ancoras.append((pos, raio_desc))

    for var in _variantes_preco(item.get("preco_unitario", "")):
        start, achadas = 0, 0
        while achadas < 2:  # até 2 ocorrências por variante (limita custo/ruído)
            p = texto_doc.find(var, start)
            if p < 0:
                break
            ancoras.append((p, raio_preco))
            start, achadas = p + len(var), achadas + 1

    if not ancoras:
        return texto_doc[: raio_desc * 4]

    intervalos = _merge_intervalos(
        [(max(0, c - r), min(len(texto_doc), c + r)) for c, r in ancoras])
    partes, total = [], 0
    for a, b in intervalos:
        if total >= janela_max:
            break
        b = min(b, a + (janela_max - total))
        partes.append(texto_doc[a:b])
        total += b - a
    return "\n[...]\n".join(partes)


def extrair_item(curador, texto_doc: str, item: dict, *, janela_max: int = MAX_JANELA,
                 raio_preco: int = RAIO_PRECO) -> dict:
    """Chama `Curador.extrair_item_pdf` na janela recortada. Devolve o shape que
    `estrategias.base.validar_extracao` espera (`descricao_completa`/`preco_unitario`/
    `quantidade`/`encontrado`)."""
    if not texto_doc.strip():
        return {"encontrado": False, "_sem_texto": True}
    janela = janela_para_item(texto_doc, item, janela_max=janela_max, raio_preco=raio_preco)
    return curador.extrair_item_pdf(janela, item)
