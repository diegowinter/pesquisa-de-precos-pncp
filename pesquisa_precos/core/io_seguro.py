"""
I/O crash-safe e utilitários de resumibilidade (seção 1.2 do guia).

Extraído do `curar_catalogo_grupos_paralelo.py` da v1 e generalizado. Todo I/O de texto
fixa `encoding="utf-8"` — é a defesa contra o bug de acentos corrompidos (`SERVI��OS`)
que vinha do encoding implícito cp1252 no Windows.

Componentes:
  - EscritorSeguro: CSV append com header automático e durabilidade por linha
    (writerow + flush + os.fsync). A latência de rede/LLM domina, então o fsync é
    desprezível e a perda efetiva em crash é ≈ zero.
  - ler_chaves_concluidas: conjunto de chaves já gravadas numa saída (para pular no resume).
  - ler_por_codigo: dicionário {chave: linha} da última ocorrência (dedup em resume).
  - ler_csv / escrever_csv: leitura/escrita utf-8 de CSV inteiro (não incremental).
"""

import csv
import os

# CSVs desta pipeline podem ter descrições longas de PDF; suba o limite de campo.
csv.field_size_limit(10 * 1024 * 1024)


class EscritorSeguro:
    """CSV append com durabilidade por linha: writerow + flush + os.fsync a cada gravação."""

    def __init__(self, caminho: str, colunas: list[str]):
        self.colunas = colunas
        os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
        novo = not (os.path.exists(caminho) and os.path.getsize(caminho) > 0)
        self.f = open(caminho, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=colunas, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        if novo:
            self.w.writeheader()
            self._sync()

    def _sync(self):
        self.f.flush()
        os.fsync(self.f.fileno())

    def escrever(self, linha: dict):
        self.w.writerow({k: linha.get(k, "") for k in self.colunas})
        self._sync()

    def fechar(self):
        try:
            self._sync()
        finally:
            self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()


def ler_chaves_concluidas(caminho: str, col_chave) -> set:
    """
    Conjunto de chaves já presentes numa saída (para pular no resume).

    col_chave: nome de coluna (str) ou tupla de colunas (chave composta). A chave devolvida
    é str para coluna única, ou tupla de str para chave composta — combine com o mesmo
    formato ao testar pertinência.
    """
    feitas: set = set()
    if not (os.path.exists(caminho) and os.path.getsize(caminho) > 0):
        return feitas
    cols = (col_chave,) if isinstance(col_chave, str) else tuple(col_chave)
    with open(caminho, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            chave = tuple(str(r.get(c, "")) for c in cols)
            feitas.add(chave[0] if len(cols) == 1 else chave)
    return feitas


def ler_por_codigo(caminho: str, col_chave=("tipo", "codigo")) -> dict:
    """Lê um CSV e devolve {chave: linha} — última ocorrência vence (dedup em resume)."""
    por_cod: dict = {}
    if not (os.path.exists(caminho) and os.path.getsize(caminho) > 0):
        return por_cod
    cols = (col_chave,) if isinstance(col_chave, str) else tuple(col_chave)
    with open(caminho, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            chave = tuple(str(r.get(c, "")) for c in cols)
            por_cod[chave[0] if len(cols) == 1 else chave] = r
    return por_cod


def ler_csv(caminho: str) -> list[dict]:
    """Lê um CSV inteiro como lista de dicts (utf-8). Arquivo ausente → lista vazia."""
    if not (os.path.exists(caminho) and os.path.getsize(caminho) > 0):
        return []
    with open(caminho, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def escrever_csv(caminho: str, colunas: list[str], linhas) -> None:
    """Escreve um CSV inteiro de uma vez (utf-8, QUOTE_ALL). Cria a pasta se preciso."""
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=colunas, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for linha in linhas:
            w.writerow({k: linha.get(k, "") for k in colunas})
