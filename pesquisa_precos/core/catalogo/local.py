"""
Grupos de segurança pública do CATMAT/CATSER — quais `codigoGrupo` a etapa 0a baixa.

LEGADO desde a Fase 10 (ADR-017): a fonte da verdade do ESCOPO é a tabela `grupo_permitido`,
editável pela interface; estas constantes só alimentam o seed da migration 0006. Mudá-las aqui
NÃO muda o que a 0a baixa — para isso, edite a curadoria na interface.

Até a Fase 13 este módulo também carregava os parquet do catálogo e filtrava por palavra-chave
em pandas. Isso morreu com o caminho CSV: quem responde "quais itens do catálogo" agora é
`db/repos/catalogo.py` em SQL, e o pré-filtro léxico é `core/pareamento/indice_lexical.py`.
"""

GRUPOS_MATERIAIS = {10, 12, 13, 15, 23, 25, 42, 58, 62, 67, 68, 70, 74, 84}
GRUPOS_SERVICOS = {841, 851, 852, 929, 931, 965}
