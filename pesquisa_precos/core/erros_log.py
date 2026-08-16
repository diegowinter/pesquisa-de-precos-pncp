"""
Log de erros da curadoria (diagnóstico).

A rodada 1 é id-only e não guarda o motivo de um `erro` no CSV principal. Este módulo
registra, num CSV à parte, a CAUSA de cada item que falhou (exceção da API, resposta fora
do formato, etc.), para depois entender o que deu errado sem re-rodar às cegas.

Append + fsync a cada linha (durável). Chamado sempre pela thread principal.
"""

import csv
import os
import time

COLUNAS_ERRO = ["quando", "rodada", "tipo", "codigo", "nome", "motivo"]


def logar_erro(caminho: str, rodada: str, tipo, codigo, nome, motivo) -> None:
    novo = not (os.path.exists(caminho) and os.path.getsize(caminho) > 0)
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS_ERRO, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        if novo:
            w.writeheader()
        w.writerow({
            "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rodada": rodada,
            "tipo": tipo,
            "codigo": codigo,
            "nome": (nome or "")[:120],
            "motivo": (str(motivo) if motivo is not None else "")[:400],
        })
        f.flush()
        os.fsync(f.fileno())
