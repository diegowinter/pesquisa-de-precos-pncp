"""
Repositórios por agregado — a única camada que fala SQL.

Regra da Fase 2 (docs/04_FASES.md): **nenhuma step escreve SQL solto**. Etapa, migração e
(depois) API passam por aqui. O motivo não é purismo: a consulta que monta o export junta seis
tabelas e um erro de join produz um preço errado no XLSX, não uma exceção. Concentrando o SQL,
existe um lugar só para revisar e um lugar só para testar.

Cada módulo cobre um agregado de docs/04_FASES.md §"Ordem sugerida dentro da fase":
`catalogo` → `termo` → `documento`+`item` → `classificacao` → `enriquecido` → `par` → `grupo`.

Duas assinaturas convivem de propósito:
  - funções que recebem `Session` (SQLAlchemy) — leitura e escrita de baixo volume;
  - funções que recebem `psycopg.Connection` — escrita em massa via `COPY` (ver `db/copy.py`).
Volume é a diferença. Um `INSERT` por linha em 1,6 milhão de itens não termina em tempo útil.
"""
