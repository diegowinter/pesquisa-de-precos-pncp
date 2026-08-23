"""
Normalização de texto e mapas de texto compartilhados pelas etapas.

Duas coisas moram aqui:

1. `normalizar_texto` / `texto_hash` — a **única** normalização do projeto. O
   `texto_hash = sha1(norm(descricao)|norm(unidade))` é calculado na ingestão (etapa 2 /
   migração m07) e consultado na classificação (etapa 3). Uma diferença mínima entre as
   duas pontas invalida o dedup permanente de `texto_classificacao` e manda 320 mil textos
   já pagos de volta ao LLM. Por isso a função é uma só e vive aqui
   (docs/08_CONVENCOES.md §5.4).

2. Mapas de texto do pareamento (6b, 6c, 7, 8): como montar o texto do item de catálogo e
   do item PNCP (com a descrição enriquecida do PDF quando houver), sem duplicar entre os
   módulos de etapa.
"""

import hashlib
import unicodedata



def normalizar_texto(valor) -> str:
    """Minúsculo, sem acento, espaços colapsados. Base do `texto_hash`.

    A dobra de acento (NFKD + descarte dos combining) é o único ponto em que esta função
    difere do `_norm` que a etapa 3 usava antes da Fase 2. O efeito é FUNDIR grupos que só
    diferem por acento ("agente lacrimogeneo" / "agente lacrimogêneo" — par que existe de
    verdade no acervo, ver checkpoints/2_conceitos_extra.csv): menos chamadas de LLM, nunca
    mais. Nenhum item já classificado é reclassificado por causa disso, porque o filtro de
    "já feito" é por item, não por hash.
    """
    txt = unicodedata.normalize("NFKD", str(valor or "").strip().lower())
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.split())


def normalizar_termo(valor) -> str:
    """Minúsculo e espaços colapsados, **preservando o acento**. Chave de dedup de `termo`.

    Divergência CONSCIENTE em relação a docs/05_MIGRACAO.md §m05, que manda tirar o acento
    também aqui. Medido no acervo real: dobrar acento colapsa os 499 termos de
    `1_conceitos_termos.csv` em 338 — porque a etapa 1 **gera de propósito** o par com e sem
    acento para todo termo (`core/classificacao/variacoes.py`: "a duplicação acento/sem-acento
    genérica é feita no chamador (etapa 1) para TODO termo"). A busca do PNCP é sensível a
    acento, então "ambulancia" e "ambulância" trazem resultados diferentes e são duas buscas,
    não uma.

    Aplicar a regra do documento apagaria 161 termos de busca em silêncio, e o sintoma seria
    "a coleta traz menos documentos que antes" meses depois, sem causa aparente. Onde dobrar
    acento é certo — comparar SIGNIFICADO de descrição de item — o projeto usa
    `normalizar_texto`.
    """
    return " ".join(str(valor or "").strip().lower().split())


def texto_hash(descricao, unidade=None) -> str:
    """sha1(norm(descricao) || '|' || norm(unidade)) — key do dedup de classificação.

    (descrição, unidade) porque são exatamente os dois campos que o classificador lê: texto
    igual ⇒ mesma classe, sem perda. Ver docs/02_SCHEMA.md §4.
    """
    base = f"{normalizar_texto(descricao)}|{normalizar_texto(unidade)}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()
