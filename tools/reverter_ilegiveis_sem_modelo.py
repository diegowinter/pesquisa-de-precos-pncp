"""Devolve à fila da etapa 5 os documentos julgados `ilegivel` sem o modelo os ter visto.

Em 2026-08-30 a etapa 5 marcou 1.274 documentos como `ilegivel`. TODOS com `hash_arquivo`
nulo — o hash é gravado no instante em que os bytes vão para o modelo, então nulo significa
que a extração nunca aconteceu. A causa era `_baixar_documento` devolver lista vazia quando o
download falhava (`baixar_arquivo` tenta 5 vezes e devolve `None` em silêncio), e a etapa
tratar isso como "documento sem tabela". Amostra de 15: os 15 tinham arquivo publicado no
PNCP e baixaram normalmente no dia seguinte.

O veredito é falso e caro: com `ilegivel` fora da fila por padrão, esses documentos ficariam
para trás sem nunca terem sido tentados de verdade.

    uv run python tools/reverter_ilegiveis_sem_modelo.py          # só mostra
    uv run python tools/reverter_ilegiveis_sem_modelo.py --aplicar

Não toca em documento com `hash_arquivo` preenchido: esse o modelo viu, e o veredito é dele.
"""
import sys

from sqlalchemy import text

from pesquisa_precos.db import session as db

ALVO = """
  FROM documento d
 WHERE d.estado = 'ilegivel'
   AND d.hash_arquivo IS NULL
"""

APLICAR = f"""
WITH alvo AS (SELECT d.numero_controle_pncp AS nc {ALVO}),
     limpa_itens AS (
         DELETE FROM item_enriquecido
          WHERE numero_controle_pncp IN (SELECT nc FROM alvo) RETURNING 1),
     limpa_extr AS (
         DELETE FROM documento_extracao
          WHERE numero_controle_pncp IN (SELECT nc FROM alvo) RETURNING 1)
UPDATE documento SET estado = 'descoberto', updated_at = now()
 WHERE numero_controle_pncp IN (SELECT nc FROM alvo)
"""


def main() -> None:
    aplicar = "--aplicar" in sys.argv
    with db.session() as s:
        n = s.execute(text(f"SELECT count(*) {ALVO}")).scalar_one()
        print(f"{n} documentos `ilegivel` que nunca chegaram ao modelo")
        if not n or not aplicar:
            print("(nada aplicado — rode com --aplicar)" if n else "")
            return
        s.execute(text(APLICAR))
        s.commit()
        print(f"{n} devolvidos para `descoberto`; extração e itens vazios removidos")


if __name__ == "__main__":
    main()
