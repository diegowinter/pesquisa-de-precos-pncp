"""
m14 — Cache de embeddings: `checkpoints/6a_emb_cache.parquet` → `embedding_cache`.

O parquet é chaveado só por `sha1(texto)`. A chave nova inclui **provedor, modelo e dimensão**
(ADR-006 §1) — sem isso, trocar de provedor mistura espaços vetoriais em silêncio, e o sintoma
seria um cosseno que parou de fazer sentido, sem nenhum erro.

Como o parquet não guarda de onde os vetores vieram, os três valores são preenchidos com o que
foi efetivamente usado na v2/v3 e ficam sobrescrevíveis por flag. **A dimensão não é informada:
é MEDIDA no próprio parquet** e comparada com o esperado — errar aqui invalidaria em silêncio
os embeddings pagos em GPU, e é o risco explicitamente apontado em docs/05_MIGRACAO.md §m14.

O vetor vai como float16 little-endian em `bytea`. Os vetores são L2-normalizados e servem só
para cosseno; meia precisão custa ~1e-3 no score e corta o armazenamento pela metade.

Uso: python -m migracao.m14_embeddings [--provedor X] [--modelo Y]
"""

import sys

import numpy as np
import pyarrow.parquet as pq
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from pesquisa_precos.config import paths
from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import par as repo
from migracao._comum import Relatorio, cabecalho, console, existe

LOTE = 5_000

# Quem gerou o cache: o servidor de GPU caseira rodando bge-m3 (ver `providers/gpu_remoto.py`
# e `embedder_local.py` — ambos gravam no MESMO parquet, com a mesma chave sha1(texto)).
PROVEDOR_PADRAO = "gpu_caseira"
DIMENSAO_ESPERADA = 1024  # bge-m3


def migrar(provedor: str = PROVEDOR_PADRAO, modelo: str | None = None) -> Relatorio:
    rel = Relatorio("m14 — cache de embeddings")
    if not existe(paths.CK_6A_EMB_CACHE):
        rel.aviso(f"{paths.CK_6A_EMB_CACHE.name} ausente — a próxima 6a reembeda tudo na GPU.")
        return rel

    modelo = modelo or carregar_config()["embedder_model"]
    arquivo = pq.ParquetFile(paths.CK_6A_EMB_CACHE)
    total = arquivo.metadata.num_rows
    rel.mais("vetores no parquet", total)

    dimensoes: set[int] = set()
    enviados = 0
    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TaskProgressColumn(), console=console) as barra, db.raw_connection() as conn:
        tarefa = barra.add_task(f"gravando ({provedor}/{modelo})", total=total)
        for bloco in arquivo.iter_batches(batch_size=LOTE, columns=["hash", "vetor"]):
            dados = bloco.to_pydict()
            itens = []
            for h, v in zip(dados["hash"], dados["vetor"]):
                vetor = np.asarray(v, dtype=np.float32)
                dimensoes.add(int(vetor.size))
                itens.append((h, vetor))
            enviados += repo.gravar_embeddings(conn, provedor, modelo, itens)
            conn.commit()
            barra.update(tarefa, completed=enviados)
    rel.mais("vetores enviados", enviados)

    # Medido, não assumido. Uma dimensão diferente da esperada significa que o parquet foi
    # gerado por outro modelo — e migrá-lo sob o nome errado é exatamente o bug silencioso
    # que a chave composta existe para evitar.
    rel.mais("dimensões distintas", len(dimensoes))
    if dimensoes != {DIMENSAO_ESPERADA}:
        rel.aviso(f"dimensão MEDIDA no parquet: {sorted(dimensoes)} (esperado "
                  f"{DIMENSAO_ESPERADA} para bge-m3). Confirme `--modelo` antes de usar este "
                  f"cache na 6a — espaço vetorial errado não dá erro, dá resultado ruim.")

    with db.session() as s:
        rel.mais("embedding_cache no banco", repo.contar(s)["embedding_cache"])
    return rel


def main() -> None:
    cabecalho("m14 — cache de embeddings", paths.CK_6A_EMB_CACHE, "embedding_cache")
    console.print(f"  banco  : {db.database_url()}")
    args = sys.argv[1:]

    def flag(nome: str, default):
        return args[args.index(nome) + 1] if nome in args else default

    migrar(provedor=flag("--provedor", PROVEDOR_PADRAO),
           modelo=flag("--modelo", None)).imprimir()


if __name__ == "__main__":
    main()
