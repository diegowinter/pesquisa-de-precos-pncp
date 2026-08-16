"""
Cliente de arquivos de contratos/atas via API de Integração PNCP, com download.

Endpoints:
  Contrato: GET /api/pncp/v1/orgaos/{cnpj}/contratos/{ano}/{seq}/arquivos
  Ata:      GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/atas/{seqAta}/arquivos

Seleciona o registro mais recente (por dataPublicacaoPncp) cujo tipoDocumentoNome
casa com o tipo alvo ("Contrato" ou "Ata de Registro de Preço") e baixa o arquivo.

Uso:
    python consultar_arquivos.py --tipo contrato --cnpj 00394494000136 --ano 2026 --seq 1073
    python consultar_arquivos.py --tipo ata --cnpj 16695025000197 --ano 2025 --seq 679 --seq-ata 1
"""

import argparse
import os
import re
import time

import requests

PNCP_BASE = "https://pncp.gov.br/api/pncp/v1/orgaos"

# tipoDocumentoNome alvo por fonte
TIPO_DOC_ALVO = {
    "contrato": "Contrato",
    "ata": "Ata de Registro de Preço",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def build_url_arquivos(tipo: str, cnpj: str, ano, seq, seq_ata=None) -> str:
    if tipo == "contrato":
        return f"{PNCP_BASE}/{cnpj}/contratos/{ano}/{seq}/arquivos"
    # ata
    return f"{PNCP_BASE}/{cnpj}/compras/{ano}/{seq}/atas/{seq_ata}/arquivos"


def listar_arquivos(tipo: str, cnpj: str, ano, seq, seq_ata=None, silent: bool = False) -> list[dict]:
    """Lista os arquivos do documento. Retorna lista vazia em 204/404."""
    url = build_url_arquivos(tipo, cnpj, ano, seq, seq_ata)
    params = {"pagina": 1, "tamanhoPagina": 500}
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=(30, 300))
            if resp.status_code in (204, 404) or not resp.content:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except requests.exceptions.JSONDecodeError:
            return []
        except Exception as e:
            wait = min(attempt * 5, 60)
            if not silent:
                print(f"[arquivos {cnpj}/{ano}/{seq}] Tentativa {attempt} falhou: {e} — retry em {wait}s")
            time.sleep(wait)


def selecionar_do_tipo(arquivos: list[dict], tipo: str) -> list[dict]:
    """
    Retorna TODOS os arquivos do tipoDocumentoNome alvo, ordenados por sequencialDocumento
    (asc — documento original primeiro, aditivos/apostilamentos/prorrogações depois).
    Atas podem ter aditivos publicados como novos documentos do mesmo tipo.
    """
    alvo = TIPO_DOC_ALVO[tipo]
    candidatos = [a for a in arquivos if (a.get("tipoDocumentoNome") or "").strip() == alvo]
    return sorted(candidatos, key=lambda a: a.get("sequencialDocumento") or 0)


def _extensao_resposta(resp: requests.Response, fallback: str = ".pdf") -> str:
    """Deduz a extensão do arquivo a partir dos headers da resposta."""
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^"]+)"?', cd)
    if m:
        _, ext = os.path.splitext(m.group(1))
        if ext:
            return ext
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    mapa = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "text/html": ".html",
    }
    return mapa.get(ctype, fallback)


def baixar_arquivo(uri: str, destino_dir: str, nome_base: str, silent: bool = False) -> str | None:
    """
    Baixa o arquivo da `uri` para `destino_dir` com nome `{nome_base}{ext}`.
    Retorna o caminho local salvo, ou None em falha persistente.
    """
    os.makedirs(destino_dir, exist_ok=True)
    attempt = 0
    while attempt < 5:
        attempt += 1
        try:
            resp = requests.get(uri, headers=HEADERS, timeout=(30, 300), stream=True)
            resp.raise_for_status()
            ext = _extensao_resposta(resp)
            caminho = os.path.join(destino_dir, f"{nome_base}{ext}")
            with open(caminho, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return caminho
        except Exception as e:
            wait = min(attempt * 5, 60)
            if not silent:
                print(f"[download {uri}] Tentativa {attempt} falhou: {e} — retry em {wait}s")
            time.sleep(wait)
    return None


def _sem_extensao(titulo: str) -> str:
    """Remove uma extensão de documento ao final do título (evita nomes tipo `x.pdf.pdf`)."""
    return re.sub(r"\.(pdf|zip|doc|docx|jpg|jpeg|png|html?)$", "", titulo.strip(), flags=re.IGNORECASE)


def baixar_arquivos(arquivos_alvo: list[dict], destino_dir_doc: str, silent: bool = False) -> list[str]:
    """
    Baixa todos os `arquivos_alvo` na subpasta `destino_dir_doc` (uma pasta por documento).
    Cada arquivo é nomeado `{sequencialDocumento}_{titulo_sanitizado_curto}{ext}`.
    Retorna a lista de NOMES de arquivo salvos (sem o diretório).
    """
    os.makedirs(destino_dir_doc, exist_ok=True)
    nomes = []
    for i, arq in enumerate(arquivos_alvo):
        uri = arq.get("uri") or arq.get("url")
        if not uri:
            continue
        seq_doc = arq.get("sequencialDocumento") or (i + 1)
        titulo = sanitizar(_sem_extensao(arq.get("titulo") or ""))[:60].strip("_") or "documento"
        nome_base = f"{seq_doc}_{titulo}"
        if i > 0:
            time.sleep(0.5)  # pausa leve entre downloads para não acionar o WAF
        caminho = baixar_arquivo(uri, destino_dir_doc, nome_base, silent=silent)
        if caminho:
            nomes.append(os.path.basename(caminho))
    return nomes


def sanitizar(texto: str) -> str:
    """Remove caracteres inválidos para nomes de arquivo."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(texto))


def main():
    parser = argparse.ArgumentParser(description="Consultar/baixar arquivo de contrato/ata - PNCP")
    parser.add_argument("--tipo", choices=["contrato", "ata"], required=True)
    parser.add_argument("--cnpj", required=True)
    parser.add_argument("--ano", required=True)
    parser.add_argument("--seq", required=True)
    parser.add_argument("--seq-ata", help="Sequencial da ata (apenas para tipo=ata)")
    parser.add_argument("-d", "--dir", default="data/arquivos")
    args = parser.parse_args()

    arquivos = listar_arquivos(args.tipo, args.cnpj, args.ano, args.seq, args.seq_ata)
    print(f"{len(arquivos)} arquivo(s) listado(s).")
    alvos = selecionar_do_tipo(arquivos, args.tipo)
    if not alvos:
        print(f"Nenhum arquivo do tipo '{TIPO_DOC_ALVO[args.tipo]}'.")
        return
    print(f"{len(alvos)} arquivo(s) do tipo '{TIPO_DOC_ALVO[args.tipo]}'.")

    identificador = sanitizar(f"{args.tipo.upper()}_{args.cnpj}_{args.ano}_{args.seq}")
    if args.tipo == "ata" and args.seq_ata:
        identificador = sanitizar(f"{identificador}_{args.seq_ata}")
    pasta_doc = os.path.join(args.dir, identificador)

    nomes = baixar_arquivos(alvos, pasta_doc)
    print(f"Salvos {len(nomes)} arquivo(s) em {pasta_doc}:")
    for n in nomes:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
