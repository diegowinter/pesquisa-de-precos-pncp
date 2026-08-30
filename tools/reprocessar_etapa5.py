"""Devolve TODOS os documentos à fila da etapa 5, apagando o que a extração quebrada produziu.

Em 2026-08-30 descobriu-se que `pdf_engine='cloudflare-ai'` — o default que este projeto
usou em todas as execucoes da etapa 5 — entrega ao modelo uma fracao do documento. Medido na
mesma ata, no mesmo minuto:

    auto (sem plugin declarado)  ->  17.878 tokens de entrada, tabela completa
    cloudflare-ai                ->      557 tokens de entrada, SEM_TABELA

O estrago nao se limita aos 1.605 `ilegivel`. Dos 2.554 que produziram tabela, 1.878 tem
menos de 2.000 chars, contra ~7.900 da mesma ata pelo caminho certo — ou seja, tabela PARCIAL.
Essa e pior que a vazia: ela passa por sucesso, e as etapas 6-8 leem `descricao_final` e
`preco_pdf` dela sem saber que faltou item.

Por isso a limpeza e total, e nao so dos ilegiveis (`reprocessar_ilegiveis` no formulario
cobriria apenas aqueles).

    uv run python tools/reprocessar_etapa5.py             # so mostra
    uv run python tools/reprocessar_etapa5.py --aplicar

Nao apaga nada de outra etapa: `item`/`item_categoria`/`documento` seguem intactos, so o
veredito da 5 sai.
"""
import sys

from sqlalchemy import text

from pesquisa_precos.db import session as db

RESUMO = """
SELECT (SELECT count(*) FROM documento_extracao) AS extracoes,
       (SELECT count(*) FROM item_enriquecido)   AS itens,
       (SELECT count(*) FROM documento WHERE estado <> 'descoberto') AS documentos
"""

APLICAR = """
DELETE FROM item_enriquecido;
DELETE FROM documento_extracao;
UPDATE documento
   SET estado = 'descoberto', hash_arquivo = NULL, updated_at = now()
 WHERE estado <> 'descoberto';
"""


def main() -> None:
    with db.session() as s:
        extracoes, itens, docs = s.execute(text(RESUMO)).one()
    print(f"documento_extracao : {extracoes}")
    print(f"item_enriquecido   : {itens}")
    print(f"documento != descoberto: {docs}")
    if "--aplicar" not in sys.argv:
        print("\n(nada aplicado — rode com --aplicar)")
        return
    with db.session() as s:
        s.execute(text(APLICAR))
        s.commit()
    print("\nlimpo. a etapa 5 volta a enxergar a fila inteira.")


if __name__ == "__main__":
    main()
