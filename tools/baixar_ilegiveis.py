"""Baixa uma AMOSTRA dos documentos que a etapa 5 marcou `ilegivel` sem erro nenhum.

São os documentos em que a extração rodou, foi paga, e voltou sem tabela — o caso que a
mensagem de erro não explica, porque não houve erro. Baixar alguns é a única forma de olhar
o PDF e decidir se o problema é escaneamento (aí é `mistral-ocr`), documento sem tabela de
itens (aí é `fora_de_escopo` e não `ilegivel`) ou outra coisa.

    uv run python tools/baixar_ilegiveis.py [quantos]

Só lê o banco e a API pública do PNCP. Não dispara etapa nem escreve nada.
"""
import sys
from pathlib import Path

from sqlalchemy import text

from pesquisa_precos.core.collection import fetch_files
from pesquisa_precos.db import session as db

DESTINO = Path("pdfs_estudo/ilegiveis")

SQL = """
SELECT d.numero_controle_pncp, d.tipo_doc::text, d.orgao_cnpj, d.ano,
       d.numero_sequencial, d.numero_sequencial_ata
  FROM documento d
  JOIN documento_extracao e ON e.numero_controle_pncp = d.numero_controle_pncp
 WHERE d.estado = 'ilegivel'
   AND coalesce(e.tabela_texto, '') = ''
   AND NOT EXISTS (SELECT 1 FROM item_error x
                    WHERE x.step = '5' AND x.key = d.numero_controle_pncp)
 ORDER BY md5(d.numero_controle_pncp)
 LIMIT :n
"""


def main() -> None:
    quantos = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    DESTINO.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        docs = s.execute(text(SQL), {"n": quantos}).all()
    if not docs:
        print("nenhum documento `ilegivel` sem erro registrado")
        return
    for nc, tipo, cnpj, ano, seq, seq_ata in docs:
        arquivos = fetch_files.listar_arquivos(
            tipo, (cnpj or "").strip(), str(ano or ""), (seq or "").strip(),
            (seq_ata or "").strip() or None, silent=True)
        alvos = fetch_files.selecionar_do_tipo(arquivos, tipo)
        if not alvos:
            print(f"{nc}: sem arquivo do tipo {tipo}")
            continue
        pasta = DESTINO / nc.replace("/", "_")
        pasta.mkdir(exist_ok=True)
        nomes = fetch_files.baixar_arquivos(alvos, str(pasta), silent=True)
        for nome in nomes:
            mb = (pasta / nome).stat().st_size / 1024 / 1024
            print(f"{nc}  {mb:6.2f} MB  {nome}")


if __name__ == "__main__":
    main()
