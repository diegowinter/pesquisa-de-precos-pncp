"""
Cliente de itens de uma compra (contrato ou ata) via API de Integração PNCP.

Endpoints (o path é o mesmo para contrato e ata):
  Quantidade: GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens/quantidade
  Itens:      GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens?pagina&tamanhoPagina

Filtra os itens com situacaoCompraItemNome == "Homologado".

Uso:
    python fetch_items.py --cnpj 16695025000197 --ano 2025 --seq 679
"""

import re
import time

import requests

PNCP_BASE = "https://pncp.gov.br/api/pncp/v1/orgaos"
TAM_PAGINA_MAX = 1000
SITUACAO_ALVO = "Homologado"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _get(url: str, params: dict | None, silent: bool, max_tentativas: int = 6):
    """GET com retry só no que faz sentido repetir. 4xx (404/410/403/…) é TERMINAL → None:
    o recurso não existe/foi removido, repetir só congela. 429 (rate-limit) e 5xx/rede
    retentam com backoff, até `max_tentativas` (depois desiste devolvendo None, sem travar)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=(30, 120))
            sc = resp.status_code
            if sc == 429:  # rate-limit: espera e tenta de novo
                if attempt >= max_tentativas:
                    return None
                wait = min(attempt * 5, 60)
                if not silent:
                    print(f"[{url}] 429 rate-limit — retry {attempt} em {wait}s")
                time.sleep(wait)
                continue
            if 400 <= sc < 500:  # 404/410/403/400…: recurso ausente/removido — não repetir
                return None
            if sc == 204 or not resp.content:
                return None
            resp.raise_for_status()  # 5xx cai no except e retenta
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            return None
        except Exception as e:
            if attempt >= max_tentativas:
                return None
            wait = min(attempt * 5, 60)
            if not silent:
                print(f"[{url}] Tentativa {attempt} falhou: {e} — retry em {wait}s")
            time.sleep(wait)


def resolver_sequencial_compra_contrato(cnpj: str, ano, seq_contrato, silent: bool = False) -> str | None:
    """
    Para um contrato, os itens ficam sob a COMPRA que o originou, cujo sequencial
    difere do sequencial do contrato. Busca os detalhes do contrato e extrai o
    sequencial da compra de `numeroControlePncpCompra` (ex.: '...-1-000547/2026' -> 547).
    """
    url = f"{PNCP_BASE}/{cnpj}/contratos/{ano}/{seq_contrato}"
    data = _get(url, None, silent)
    if not isinstance(data, dict):
        return None
    ctrl = data.get("numeroControlePncpCompra")
    if not ctrl:
        return None
    m = re.match(r"^\d+-\d+-(\d+)/\d{4}$", ctrl)
    if not m:
        return None
    return str(int(m.group(1)))


def fetch_resultado_vencedor(cnpj: str, ano, seq, numero_item, silent: bool = False) -> dict | None:
    """
    Resultado HOMOLOGADO vencedor de um item (o preço realmente adjudicado).

    O endpoint /itens/{n}/resultados devolve uma lista (um por fornecedor/lance). O vencedor
    é o de menor `ordemClassificacaoSrp` (1 = 1º colocado). Devolve o dict do vencedor (com
    valorUnitarioHomologado, valorTotalHomologado, quantidadeHomologada, fornecedor, data),
    ou None quando não há resultado (ex.: item sem homologação registrada → cai no estimado).
    """
    url = f"{PNCP_BASE}/{cnpj}/compras/{ano}/{seq}/itens/{numero_item}/resultados"
    data = _get(url, None, silent)
    lista = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
    if not lista:
        return None

    def _ordem(r: dict):
        o = r.get("ordemClassificacaoSrp")
        return o if isinstance(o, (int, float)) else 9999
    # prioriza quem tem valorUnitarioHomologado válido; entre eles, menor ordem SRP.
    validos = [r for r in lista if r.get("valorUnitarioHomologado") not in (None, "", 0)]
    escolha = min(validos or lista, key=_ordem)
    return escolha


def obter_quantidade(cnpj: str, ano, seq, silent: bool = False) -> int:
    url = f"{PNCP_BASE}/{cnpj}/compras/{ano}/{seq}/itens/quantidade"
    data = _get(url, None, silent)
    if data is None:
        return 0
    # endpoint pode retornar número direto ou objeto
    if isinstance(data, int):
        return data
    if isinstance(data, dict):
        for k in ("quantidade", "data", "total"):
            v = data.get(k)
            if isinstance(v, int):
                return v
    try:
        return int(data)
    except (TypeError, ValueError):
        return 0


def fetch_itens(cnpj: str, ano, seq, silent: bool = False) -> list[dict]:
    """Obtém todos os itens da compra (quantidade-aware)."""
    quantidade = obter_quantidade(cnpj, ano, seq, silent=silent)
    if quantidade <= 0:
        # fallback: tenta uma página padrão mesmo sem quantidade
        quantidade = TAM_PAGINA_MAX

    itens: list[dict] = []
    tam_pagina = min(quantidade, TAM_PAGINA_MAX)
    total_paginas = (quantidade + tam_pagina - 1) // tam_pagina if tam_pagina else 1

    base = f"{PNCP_BASE}/{cnpj}/compras/{ano}/{seq}/itens"
    for pagina in range(1, total_paginas + 1):
        data = _get(base, {"pagina": pagina, "tamanhoPagina": tam_pagina}, silent)
        if data is None:
            break
        lote = data if isinstance(data, list) else data.get("data", [])
        if not lote:
            break
        itens.extend(lote)
        if len(lote) < tam_pagina:
            break
    return itens


def filtrar_homologados(itens: list[dict]) -> list[dict]:
    return [i for i in itens if (i.get("situacaoCompraItemNome") or "").strip() == SITUACAO_ALVO]
