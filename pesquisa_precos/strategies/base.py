"""
Base comum das estratégias de extração da etapa 5 (Fase 8, ADR-010).

`janela`, `completa` e `visao` produzem o MESMO formato intermediário — um dict por item com
`descricao_completa`/`preco_unitario`/`quantidade`/`encontrado` — e por isso compartilham a
mesma validação. É o requisito de docs/02_SCHEMA.md §6.2: a `completa` (e a `visao`) têm que
herdar da `janela` a confirmação por quantidade, a banda de sanidade de preço e a derivação de
`doc_status`. Ter as três aqui, uma vez só, é o que garante que nenhuma estratégia nova
"esquece" um desses requisitos.
"""

from collections import defaultdict

# Match exato de preço (até os centavos) acima deste valor já é fingerprint único: confirma
# o item mesmo sem casar a quantidade (contratos de serviço têm qtd=1 e o doc não a reafirma).
PRECO_FINGERPRINT = 1000.0

# Banda de sanidade: o preço homologado costuma ficar em [0,3× … 3×] do estimado da API. Fora
# disso é quase sempre misparse de milhar/decimal (ou item errado) — mantém a descrição, mas
# marca o preço como suspeito em vez de aceitar cegamente.
BANDA_SANIDADE_MIN = 0.3
BANDA_SANIDADE_MAX = 3.0


def num(v) -> float | None:
    """Converte número BR ('1.234,56') ou US ('1234.56')/cru para float. None se não der."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if "," in s:  # vírgula = separador decimal (BR): pontos são milhar
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def validar_extracao(extraido: dict, item: dict) -> tuple[str, float | None, float | None]:
    """Confirma que a extração achou o item CERTO e devolve (status, preco_pdf, divergencia).

    Contrato de `extraido`: {"descricao_completa", "preco_unitario", "quantidade",
    "encontrado"} — o mesmo para as três estratégias (a `janela` pede isso direto ao LLM; a
    `completa`/`visao` chegam aqui depois de `casar_item_tabela`, que devolve o mesmo shape).

    O item é confirmado pela QUANTIDADE (fingerprint anti-colisão/PDF-trocado) OU por um match
    exato de preço alto. Confirmado o item, o PREÇO deixa de ser critério de aceite e vira
    SAÍDA: a API traz o valor estimado, o PDF o valor homologado/registrado (o que interessa).
    Divergência é sinalizada, não descartada (docs/08_CONVENCOES.md §5.9).

    Valores de `status` batem 1:1 com o enum `status_enriquecimento` de docs/02_SCHEMA.md §2:
      pdf_ok / pdf_ok_diverge / pdf_ok_preco_suspeito / pdf_ok_sem_preco / pdf_ok_sem_ref /
      qtd_nao_confere / nao_encontrado.
    """
    if not (extraido.get("encontrado") and extraido.get("descricao_completa")):
        return "nao_encontrado", None, None
    pe, pa = num(extraido.get("preco_unitario")), num(item.get("preco_unitario"))
    qe, qa = num(extraido.get("quantidade")), num(item.get("quantidade"))
    qtd_ok = qe is not None and qa is not None and abs(qe - qa) <= max(1.0, 0.01 * abs(qa))
    preco_ok = pe is not None and pa is not None and pa != 0 and abs(pe - pa) / abs(pa) <= 0.01
    confirmado = qtd_ok or (preco_ok and pa is not None and abs(pa) >= PRECO_FINGERPRINT)
    if not confirmado:
        return "qtd_nao_confere", None, None
    if pe is None or pe <= 0:
        return "pdf_ok_sem_preco", None, None
    if pa is None or pa == 0:
        return "pdf_ok_sem_ref", pe, None
    if preco_ok:
        return "pdf_ok", pe, 0.0
    div = round((pe - pa) / pa, 4)
    if pe < BANDA_SANIDADE_MIN * abs(pa) or pe > BANDA_SANIDADE_MAX * abs(pa):
        return "pdf_ok_preco_suspeito", pe, div
    return "pdf_ok_diverge", pe, div


def casar_itens_contra_tabela(curador, itens: list[dict], tabela: list[dict]) -> dict[str, dict]:
    """Casa cada item sobrevivente do documento contra a TABELA já extraída (`completa`/`visao`),
    via `Curador.casar_item_tabela` (mesmo método da antiga 5_alt_b). Devolve
    `item_key -> extraido` no shape que `validar_extracao` espera."""
    out: dict[str, dict] = {}
    if not tabela:
        for item in itens:
            out[item["item_key"]] = {"encontrado": False}
        return out
    for item in itens:
        match = curador.casar_item_tabela(item, tabela)
        idx = match.get("idx", -1)
        fornecedor = tabela[idx].get("fornecedor", "") if 0 <= idx < len(tabela) else ""
        out[item["item_key"]] = {
            "descricao_completa": match.get("descricao_completa") or "",
            "preco_unitario": match.get("preco_unitario"),
            "quantidade": match.get("quantidade"),
            "encontrado": bool(match.get("encontrado")),
            "_fornecedor": fornecedor,
        }
    return out


def doc_status_de_motivos(status_por_item: dict[str, str]) -> str:
    """Detector de PDF trocado (docs/03_ETAPAS.md §5.1): deriva `doc_status` do documento
    INTEIRO a partir do status de todos os seus itens — nenhum item confirmou = `suspeito`
    (o PDF provavelmente não é o documento certo); nenhuma página produziu texto útil (todos
    `sem_texto`) = `ilegivel`; senão `ok`."""
    motivos = list(status_por_item.values())
    if not motivos:
        return "ilegivel"
    n_ok = sum(m.startswith("pdf_ok") for m in motivos)
    n_legivel = sum(m != "sem_texto" for m in motivos)
    if n_legivel == 0:
        return "ilegivel"
    if n_ok == 0:
        return "suspeito"
    return "ok"


def destino_de(status: str, doc_status: str) -> str:
    """manter (confirmado) / revisar (doc suspeito ou ilegível) / descartar (falha isolada
    num documento saudável) — docs/03_ETAPAS.md §5.1."""
    if status.startswith("pdf_ok"):
        return "manter"
    if doc_status in ("suspeito", "ilegivel"):
        return "revisar"
    return "descartar"


def agrupar_por_doc_status(itens_por_doc: dict[str, list[str]],
                           status_por_item: dict[str, str]) -> dict[str, str]:
    """`numero_controle_pncp -> doc_status`, uma vez por documento (não por item)."""
    out = {}
    for doc, item_keys in itens_por_doc.items():
        out[doc] = doc_status_de_motivos({ik: status_por_item.get(ik, "sem_texto") for ik in item_keys})
    return out


def contagem_destinos(linhas: list[dict]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for l in linhas:
        c[l["destino"]] += 1
        if l["status"] == "pdf_ok_diverge":
            c["preco_diverge"] += 1
        elif l["status"] == "pdf_ok_preco_suspeito":
            c["preco_suspeito"] += 1
        elif l["status"] == "pdf_ok_sem_preco":
            c["sem_preco"] += 1
    return dict(c)
