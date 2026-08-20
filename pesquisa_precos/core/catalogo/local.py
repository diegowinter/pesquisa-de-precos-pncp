"""
Carga e filtragem local do catálogo (CATMAT/CATSER) com Pandas.

A API de dados abertos do catálogo NÃO tem busca textual (só filtra por códigos),
então o casamento descrição→item é feito localmente sobre o catálogo já baixado
(ver `etapas/e0a_catalogo.py`). Este módulo:

  - carrega os parquet de materiais/serviços;
  - filtra por grupos (default = grupos de segurança pública, overridável);
  - pré-filtra por palavra-chave (case/acento-insensível) a partir de uma descrição.

Os grupos de segurança são copiados de itens-cat-v3/4_classificar_itens_llm.py.

Uso (teste isolado):
    python -m pesquisa_precos.core.catalogo.local --descricao "pistola"
    python -m pesquisa_precos.core.catalogo.local --descricao "colete balistico" --tipo material
"""

import argparse
import re
import unicodedata

import pandas as pd

from pesquisa_precos.config import paths

# ── Grupos de segurança pública (allow-list de codigoGrupo) ─────────────────────
# LEGADO desde a Fase 10 (ADR-017): a fonte da verdade é a tabela `grupo_permitido`, editável
# pela interface. Estas constantes só alimentam o caminho `--fonte csv` e o seed da migration
# 0006 — mudá-las aqui NÃO muda o que a 0a baixa com `--fonte banco` (o default).
# Copiados de itens-cat-v3/4_classificar_itens_llm.py (GRUPOS_MATERIAIS/SERVICOS).
# Grupo 25 entrou junto com o PDM 1665 (ACESSÓRIO CARRO BLINDADO), que vive na classe 2590.
GRUPOS_MATERIAIS = {10, 12, 13, 15, 23, 25, 42, 58, 62, 67, 68, 70, 74, 84}
GRUPOS_SERVICOS = {841, 851, 852, 929, 931, 965}

# ── Allow-list curada do catálogo (substitui a curadoria por LLM) ────────────────
# LEGADO desde a Fase 10 (ADR-017): a fonte da verdade é a tabela `pdm_permitido`. Ver o aviso
# dos grupos acima — vale igual aqui.
# Em vez de curar o catálogo com LLM, filtramos direto por códigos escolhidos a dedo:
#   - materiais: por codigoPdm
#   - serviços:  por codigoServico
PDMS_MATERIAIS = {
    2995, 2994, 16657, 1423, 17996, 1425, 18582, 8079, 11255, 15345,
    1243, 13849, 14570, 15692, 1024, 1431, 13401, 4452, 4453, 6661,
    8435, 19246, 224, 226, 238, 10293, 216, 13174, 17653, 3142,
    14419, 16796, 14411, 14329, 648, 10218, 14415, 14408, 653, 16741,
    # Veículos de segurança pública que faltavam na classe 2320 (+ 2590 p/ blindado).
    # "Viatura" não é PDM no CATMAT: a viatura é comprada como veículo comum e a
    # caracterização vem na descrição específica do órgão.
    805,    # VEÍCULO TRANSPORTE PRESO
    4595,   # CARRO BLINDADO
    1665,   # ACESSÓRIO CARRO BLINDADO (grupo 25 / classe 2590)
    16793,  # VEÍCULO ESPECIAL
    2396,   # AMBULÂNCIA
    # Deliberadamente FORA: 685 (VEÍCULO AUTOMOTIVO - PICAPE) e 4613 (CARRO FORTE),
    # PDMs legados sem nenhum item ativo — o vivo p/ picape é o 14419.
}
CODIGOS_SERVICOS = {
    10014, 18384, 23809, 23817, 23833, 23841, 23825, 23850, 23868, 19631, 22870,
}

PARQUET_MATERIAIS = paths.E0A_PARQUET_MATERIAIS
PARQUET_SERVICOS = paths.E0A_PARQUET_SERVICOS

# Colunas de texto usadas no casamento por palavra-chave, por tipo.
CAMPOS_TEXTO = {
    "material": ["descricaoItem", "nomePdm", "nomeClasse", "nomeGrupo"],
    "servico": ["nomeServico", "nomeSubclasse", "nomeClasse", "nomeGrupo"],
}

# Stopwords PT comuns — irrelevantes para o casamento (removidas dos tokens).
STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "ou", "a", "o", "as", "os", "um", "uma",
    "para", "com", "sem", "por", "em", "no", "na", "nos", "nas", "ao", "aos",
    "tipo", "material", "modelo", "cor", "medida", "tamanho",
}

# Unidades para colapsar espaço entre número e unidade (ex.: "9 mm" -> "9mm").
_UNIDADES = r"mm|cm|km|kg|mg|ml|pol|un|g|m|l"
_RE_NUM_UNIDADE = re.compile(rf"(\d)\s+({_UNIDADES})\b")


def _normalizar(texto: str) -> str:
    """minúsculas + remoção de acentos + colapso de número/unidade, p/ comparação robusta."""
    if not isinstance(texto, str):
        texto = "" if texto is None else str(texto)
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    baixa = sem_acento.lower()
    return _RE_NUM_UNIDADE.sub(r"\1\2", baixa)


def tokenizar(descricao: str) -> list[str]:
    """
    Quebra a descrição em tokens significativos (normalizados, >= 2 chars, sem stopwords).
    Se sobrar vazio (só stopwords), devolve os tokens originais para não zerar a busca.
    """
    norm = _normalizar(descricao)
    brutos = [t for t in re.split(r"[^0-9a-z]+", norm) if len(t) >= 2]
    significativos = [t for t in brutos if t not in STOPWORDS]
    return significativos or brutos


def carregar_catalogo(tipo: str) -> pd.DataFrame:
    """Carrega o parquet do catálogo (tipo = 'material' | 'servico')."""
    caminho = PARQUET_MATERIAIS if tipo == "material" else PARQUET_SERVICOS
    if not caminho.exists():
        raise FileNotFoundError(
            f"Catálogo de {tipo} não encontrado em {caminho}. "
            f"Rode 'python -m pesquisa_precos.etapas.e0a_catalogo' primeiro."
        )
    return pd.read_parquet(caminho)


def filtrar_curado(tipo: str, df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Filtra o catálogo pela allow-list curada (PDMS_MATERIAIS / CODIGOS_SERVICOS).

    tipo = 'material' → mantém linhas com codigoPdm na allow-list.
    tipo = 'servico'  → mantém linhas com codigoServico na allow-list.
    `df` opcional (default: carrega o parquet do tipo).
    """
    if df is None:
        df = carregar_catalogo(tipo)
    coluna = "codigoPdm" if tipo == "material" else "codigoServico"
    permitidos = PDMS_MATERIAIS if tipo == "material" else CODIGOS_SERVICOS
    if coluna not in df.columns:
        raise KeyError(f"Coluna {coluna!r} ausente no catálogo de {tipo}.")
    codigos = pd.to_numeric(df[coluna], errors="coerce")
    return df[codigos.isin(permitidos)].reset_index(drop=True)


def filtrar_por_grupos(df: pd.DataFrame, grupos: set[int] | None) -> pd.DataFrame:
    """Mantém apenas linhas cujo codigoGrupo está em `grupos` (None = sem filtro)."""
    if grupos is None or "codigoGrupo" not in df.columns:
        return df
    codigos = pd.to_numeric(df["codigoGrupo"], errors="coerce")
    return df[codigos.isin(grupos)]


def _coluna_texto_normalizada(df: pd.DataFrame, tipo: str) -> pd.Series:
    """Concatena e normaliza os campos de texto relevantes numa única série."""
    campos = [c for c in CAMPOS_TEXTO[tipo] if c in df.columns]
    combinado = df[campos].fillna("").astype(str).agg(" ".join, axis=1)
    return combinado.map(_normalizar)


def filtrar_por_palavra_chave(
    df: pd.DataFrame, descricao: str, tipo: str, limite: int | None = None
) -> pd.DataFrame:
    """
    Pré-filtro recall-generoso: mantém linhas que contêm PELO MENOS UM token
    significativo da descrição (OR), ordenadas por nº de tokens casados (desc) —
    assim os melhores casamentos (mais próximos de um AND) vêm primeiro. A LLM faz
    o corte fino depois. `limite` corta os N melhores candidatos (controla custo LLM).

    Match case/acento-insensível por substring de token.
    """
    tokens = tokenizar(descricao)
    if not tokens:
        return df.iloc[0:0]  # descrição sem tokens úteis → nada

    texto = _coluna_texto_normalizada(df, tipo)
    score = pd.Series(0, index=df.index)
    for tok in tokens:
        score += texto.str.contains(re.escape(tok), regex=True, na=False).astype(int)

    achados = df[score > 0]
    ordenado = (
        achados.assign(_score=score[score > 0])
        .sort_values("_score", ascending=False, kind="mergesort")  # estável
        .drop(columns="_score")
    )
    return ordenado.head(limite) if limite else ordenado


def candidatos_para_descricao(
    df_grupos: pd.DataFrame, descricao: str, tipo: str, limite: int | None = None
) -> pd.DataFrame:
    """Atalho: pré-filtro por palavra-chave sobre um df já filtrado por grupos."""
    return filtrar_por_palavra_chave(df_grupos, descricao, tipo, limite=limite)


def main():
    parser = argparse.ArgumentParser(description="Teste de filtro local do catálogo")
    parser.add_argument("--descricao", required=True)
    parser.add_argument("--tipo", choices=["material", "servico"], default="material")
    parser.add_argument("--todos-grupos", action="store_true", help="Não filtrar por grupo")
    args = parser.parse_args()

    grupos = None
    if not args.todos_grupos:
        grupos = GRUPOS_MATERIAIS if args.tipo == "material" else GRUPOS_SERVICOS

    df = carregar_catalogo(args.tipo)
    df = filtrar_por_grupos(df, grupos)
    candidatos = candidatos_para_descricao(df, args.descricao, args.tipo)

    print(f"Grupos filtrados: {'(todos)' if grupos is None else sorted(grupos)}")
    print(f"Candidatos para '{args.descricao}': {len(candidatos)}")
    cols = [c for c in ("codigoItem", "codigoServico", "nomePdm", "nomeServico", "descricaoItem") if c in candidatos.columns]
    print(candidatos[cols].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
