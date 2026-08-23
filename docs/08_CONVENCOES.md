# 08 — Convenções de código

Leitura obrigatória para qualquer agente ou desenvolvedor que for escrever código neste projeto.

## 1. Idioma

- **Domínio em português**: nomes de tabela, coluna, enum, função de negócio, variável de domínio,
  mensagem de log, docstring, comentário. `item_enriquecido`, `calcular_divergencia`, `sobrevivente`.
- **Inglês só onde é convenção da linguagem/biblioteca**: `class`, `def`, `session`, `router`,
  `test_*`, `Protocol`, `BaseModel`.
- Não misturar dentro do mesmo identificador. `parse_documento` (ok, `parse` é jargão consolidado);
  `getItemEnriquecido` (não).

Motivo: o domínio é jurídico-administrativo brasileiro (CATMAT, ata de registro de preço,
homologação). Traduzir esses conceitos perde precisão e dificulta a auditoria humana.

## 2. Nomenclatura

| Elemento | Convenção | Exemplo |
|---|---|---|
| Módulo, função, variável | `snake_case` | `extrair_item_pdf` |
| Classe | `PascalCase` | `ContextoExecucao` |
| Constante | `UPPER_SNAKE` | `PRECO_FINGERPRINT` |
| Tabela | singular, `snake_case` | `item`, `documento_pagina` |
| Enum (tipo) | singular | `status_etapa` |
| Enum (valor) | `snake_case` | `pdf_ok_diverge` |
| Módulo de etapa | `e<chave>_<verbo>.py` | `e6a_pairs.py` |
| Chave de config | prefixo da etapa quando específica | `e5.janela_max` |

## 3. Estilo e ferramentas

- Python 3.12+. `ruff` (lint + format), linha de 100 colunas.
- Type hints obrigatórios em assinaturas públicas. `mypy` em modo permissivo no início,
  apertando por módulo.
- `pydantic` v2 para todo schema de entrada/saída.
- `sqlalchemy` 2.x (estilo declarativo novo), `alembic` para migrations.
- Sem `print()` em código de produção — usar `ctx.log()` ou o logger estruturado.

## 4. Docstrings

O código atual tem uma qualidade rara: **docstrings de módulo que explicam o porquê, não só o o
quê**. Isso é um ativo do projeto. Preservar e continuar.

Modelo a seguir (extraído do estilo já existente):

```python
"""
Etapa 5b — Extração guiada por item a partir do texto + destino (paralelo, resumível).

Lê o texto já parseado/OCR'd e, para cada item sobrevivente, pede ao LLM só AQUELE item
(janela multi-âncora: descrição + preço). CONFIRMA o item pela QUANTIDADE (ou por preço
exato alto) e então CAPTURA o preço do PDF como valor real: a API traz o valor estimado,
o PDF o homologado/registrado. Preço diferente é sinalizado, NÃO descartado.

Entradas: ...
Saídas: ...
Chave de resumo: item_key
"""
```

Regras:
- **Comentário explica decisão, não mecânica.** `# soma 1` é ruído; `# em contratos grandes a
  descrição e o valor ficam em seções distantes — ancorar só na descrição perdia o preço` é ouro.
- Quando um comentário registra um bug já corrigido, **manter** e marcar. Ex.: o corte em
  streaming da 6a e o `MemoryError`.
- Docstring de módulo de etapa deve declarar: o que faz, entradas, saídas, chave de resumo,
  e o que **não** fazer.

## 5. Regras que um agente pode quebrar sem perceber

Estas já causaram problema real ou representam risco alto. Cada uma tem um teste associado.

### 5.1 Corte em streaming na etapa 6a
O corte top-K + piso por código é aplicado **durante** a geração dos pares (numpy nas matrizes de
score), nunca depois de materializar o produto cartesiano em DataFrame. Um `aplicar_corte`
pós-hoc com `groupby().rank()` já causou `MemoryError` real com ~33M linhas.
O arquivo `data/6a_pares_candidatos_PRECORTE.csv` (3,7 GB) é o fóssil desse bug.

### 5.2 Nunca ler CSV grande inteiro
`2_itens_coletados.csv` = 746 MB. `5_pdf_texto.csv` = 2,6 GB. Sempre streaming
(`csv.DictReader` + lotes). E `csv.field_size_limit(10**9)` — há campos gigantes.

### 5.3 Resultado e estado no mesmo commit
Gravar o resultado de uma unidade e marcá-la como concluída em transações separadas faz a
retomada pagar o LLM de novo. Sempre no mesmo `with db.begin()`.

### 5.4 Normalização de texto tem que ser a mesma em todo lugar
`texto_hash = sha1(norm(descricao)|norm(unidade))` é calculado na ingestão (etapa 2) e consultado
na classificação (etapa 3) e na migração. Uma diferença mínima em `norm` invalida o dedup e
reclassifica 320k textos já pagos. **Uma única função, em `core/textos.py`.**

### 5.5 Chave do cache de embedding inclui provedor e modelo
`(texto_hash, provedor, modelo, dimensao)`. Chavear só por texto mistura espaços vetoriais em
silêncio quando o provedor muda.

### 5.6 Bumpar `VERSAO_CODIGO` ao mudar a lógica de uma etapa
Sem isso o fingerprint não muda e as etapas dependentes não são marcadas `desatualizada` — o
usuário recebe resultado inconsistente sem aviso. **Modo de falha conhecido.**

### 5.7 `top_n=0` significa sem teto
Não é "zero itens". Mais de 5 itens por código é esperado. Ver [ADR-016](07_DECISOES.md#adr-016).

### 5.8 Preço nunca é `float`
`numeric(18,4)` no banco, `Decimal` no Python nas fronteiras. `float` em preço de contrato
público é bug esperando acontecer.

### 5.9 Divergência de preço é sinal, não erro
A API traz o valor **estimado**; o PDF traz o **homologado/registrado** — que é o que interessa.
Divergência é gravada e sinalizada, **nunca** motivo para descartar o item.

### 5.10 Chave de API não vai para o banco
`provedor.api_key_ref` guarda o **nome** da variável de ambiente. O valor fica no `.env`,
que está no `.gitignore` e nunca é commitado.

## 6. Testes

Não buscar cobertura. Focar onde bug silencioso vira **preço errado no export** — o dano real
do projeto.

### Prioridade máxima
1. **Parsers da API do PNCP** — o contrato muda sem aviso. Testes com fixtures de resposta real
   gravadas.
2. **Extração de item a partir de texto** — `janela_para_item`, `validar_extracao`,
   `_variantes_preco`, `_num`. São funções puras: teste de tabela com casos reais, incluindo
   números BR malformados (`107.222,00` → não pode virar `107,22`).
3. **Agrupamento e menor preço** — outlier IQR, faixas, ranking.
4. **Normalização de texto / `texto_hash`** — property test: mesma entrada, mesmo hash, sempre.

### Prioridade média
- Roteamento de estratégia da etapa 5 (a fórmula).
- Cálculo de fingerprint e detecção de `desatualizada`.
- Resolução de parâmetros em camadas.
- Contabilidade de custo e teto.

### Não testar
Templates, formatação de log, wrappers finos de rota.

### Fixtures
`tests/fixtures/` com amostras reais e pequenas: 20 documentos, 200 itens, 50 pares.
Extraídas do acervo, anonimizadas apenas se necessário (são dados públicos).

### Suite de regressão de qualidade (Fase 9)
~200 itens com rótulo conhecido, extraídos de `rotulo`. Roda contra qualquer mudança de modelo,
prompt ou threshold e reporta precisão/recall. É o que permite trocar de provedor sem ser no
escuro.

## 7. Migrations

- Uma migration por mudança lógica, com `upgrade` **e** `downgrade`.
- Nunca editar migration já aplicada em produção.
- Mudança que exige backfill: migration de schema + script em `migracao/` separado e resumível.
- Antes de qualquer migration destrutiva: `pg_dump`.

## 8. Git

- Branch por fase (`fase-2-banco`), commits pequenos e descritivos em português.
- Tag ao fim de cada fase (`fase-1-final`) — é o ponto de rollback documentado em
  [05_MIGRACAO.md §7](05_MIGRACAO.md#7-roteiro-de-rollback).
- `.env` nunca é commitado. `.env.example` sim, com os nomes das chaves e valores vazios.
- Não commitar nada de `data/`.

## 9. Como um agente deve trabalhar neste repositório

1. **Ler `docs/` antes de codar.** Especialmente [07_DECISOES.md](07_DECISOES.md) — mudar uma
   decisão exige argumentar contra o contexto registrado, não só propor algo diferente.
2. **Não expandir escopo.** A lista de fora de escopo em
   [04_FASES.md](04_FASES.md#fora-de-escopo-todas-as-fases) é explícita justamente para isso.
3. **Não rodar a pipeline.** Quem executa é o usuário — é human-in-the-loop por desenho.
   Leitura e diagnóstico (inspecionar CSV, consultar banco, `tools/`) são livres.
4. **Preservar comentários que registram bugs corrigidos.** Eles são a memória do projeto.
5. **Ao portar um script atual, ler o script inteiro antes.** Há validações sutis (confirmação por
   quantidade, banda de sanidade, detector de PDF trocado) que não aparecem no nome da função e
   cuja perda só seria notada meses depois, no dado errado.
6. **Quando medir algo, medir no acervo real.** Os números em [02_SCHEMA.md §1](02_SCHEMA.md#1-dimensionamento-medido-no-acervo-atual-2026-08-16)
   vieram de contagem, não de estimativa. Manter esse padrão.
