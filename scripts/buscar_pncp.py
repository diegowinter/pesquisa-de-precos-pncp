"""
Cliente do endpoint de busca textual do PNCP.

Endpoint: GET https://pncp.gov.br/api/search/
    ?q={termo}&tipos_documento={contrato|ata}&ordenacao=-data
    &pagina={p}&tam_pagina={n}&status=vigente

Faz yield de cada resultado conforme as páginas chegam (generator), paginando
até cobrir o campo `total` retornado na primeira página.

Uso:
    python buscar_pncp.py --termo "caneta" --tipo contrato
"""

import argparse
import json
import time

import requests

BASE_URL = "https://pncp.gov.br/api/search/"
TAM_PAGINA_DEFAULT = 500
SLEEP_ENTRE_PAGINAS = 1.5  # segundos — evitar bloqueio por WAF do PNCP
MAX_TENTATIVAS = 6         # teto de retries por página (só p/ erros transientes)
# O PNCP (Elasticsearch) rejeita paginação além de ~10k resultados (offset > janela) com 400.
MAX_RESULT_WINDOW = 10000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def fetch_page(termo: str, tipo: str, pagina: int, tam_pagina: int) -> dict:
    """Obtém uma única página de busca. Retorna o JSON com `items` e `total`."""
    params = {
        "q": termo,
        "tipos_documento": tipo,
        "ordenacao": "-data",
        "pagina": pagina,
        "tam_pagina": tam_pagina,
        "status": "vigente",
    }
    for attempt in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=(30, 300))
            if resp.status_code == 204 or not resp.content:
                return {"items": [], "total": 0}
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            print(
                f"[busca '{termo}' {tipo} pág {pagina}] Corpo inválido — "
                f"status={resp.status_code} content={resp.text[:300]!r}"
            )
            return {"items": [], "total": 0}
        except requests.exceptions.HTTPError:
            code = resp.status_code
            # Só 429 (rate limit) e 5xx são transientes. Demais 4xx (400/403/404/422) são
            # permanentes — retentar é inútil (ex.: 400 = paginação além da janela do PNCP).
            if code != 429 and not (500 <= code < 600):
                print(f"[busca '{termo}' {tipo} pág {pagina}] erro {code} não-retentável — pulando página.")
                return {"items": [], "total": 0}
            wait = min(attempt * 5, 60)
            print(f"[busca '{termo}' {tipo} pág {pagina}] {code} (transiente) "
                  f"tentativa {attempt}/{MAX_TENTATIVAS} — retry em {wait}s")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = min(attempt * 5, 60)
            print(f"[busca '{termo}' {tipo} pág {pagina}] rede: {e} "
                  f"tentativa {attempt}/{MAX_TENTATIVAS} — retry em {wait}s")
            time.sleep(wait)
    print(f"[busca '{termo}' {tipo} pág {pagina}] esgotou {MAX_TENTATIVAS} tentativas — pulando página.")
    return {"items": [], "total": 0}


def iter_resultados(termo: str, tipo: str, tam_pagina: int = TAM_PAGINA_DEFAULT, on_total=None):
    """
    Generator: faz yield de cada resultado da busca conforme as páginas chegam.
    `on_total(n)` é chamado com o `total` assim que a primeira página chega.
    Pagina até cobrir todos os resultados.
    """
    first = fetch_page(termo, tipo, 1, tam_pagina)
    total = first.get("total", 0) or 0

    # PNCP não pagina além de MAX_RESULT_WINDOW resultados; o alcançável é o mínimo entre
    # o total e a janela. Reporta o alcançável (não o total) p/ a barra de progresso bater.
    paginas_max = max(1, MAX_RESULT_WINDOW // tam_pagina) if tam_pagina else 1
    alcancavel = min(total, paginas_max * tam_pagina)
    if on_total:
        on_total(alcancavel)
    if total > alcancavel:
        print(f"[busca '{termo}' {tipo}] {total} resultados, mas o PNCP só pagina "
              f"{alcancavel} (janela de {MAX_RESULT_WINDOW}). Coletando os mais recentes.")

    for item in first.get("items", []):
        yield item

    total_paginas = (total + tam_pagina - 1) // tam_pagina if tam_pagina else 1
    ultima = min(total_paginas, paginas_max)
    for pagina in range(2, ultima + 1):
        time.sleep(SLEEP_ENTRE_PAGINAS)
        data = fetch_page(termo, tipo, pagina, tam_pagina)
        for item in data.get("items", []):
            yield item


def main():
    parser = argparse.ArgumentParser(description="Busca textual no PNCP (contratos/atas)")
    parser.add_argument("--termo", required=True)
    parser.add_argument("--tipo", choices=["contrato", "ata"], required=True)
    parser.add_argument("--tam-pagina", type=int, default=TAM_PAGINA_DEFAULT)
    parser.add_argument("--limite", type=int, default=5, help="Quantos imprimir (debug)")
    args = parser.parse_args()

    total_holder = {}
    resultados = []
    for r in iter_resultados(args.termo, args.tipo, args.tam_pagina, on_total=lambda n: total_holder.__setitem__("total", n)):
        resultados.append(r)
        if len(resultados) >= args.limite:
            break

    print(f"Total reportado pelo endpoint: {total_holder.get('total')}")
    print(json.dumps(resultados[: args.limite], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
