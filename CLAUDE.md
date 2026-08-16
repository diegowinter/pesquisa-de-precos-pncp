# CLAUDE.md — guia de contexto para este repositório

Este é o repositório **oficial** da pesquisa de preços PLASEG via PNCP. Antes dele, o projeto
viveu como `itens-contratos-atas` (v1) e `itens-contratos-atas-v2` dentro do repo
`itens-via-script`; em 2026-08-16 a pasta `itens-contratos-atas-v3` foi **movida** (não copiada)
para cá e virou a raiz deste projeto. O desenho técnico completo do pipeline está em
[README.md](README.md) e [GUIA_IMPLEMENTACAO_PIPELINE.md](GUIA_IMPLEMENTACAO_PIPELINE.md) — leia
os dois antes de mexer em qualquer etapa. Este arquivo cobre o que eles não cobrem: como
trabalhar com o usuário, decisões já tomadas e o estado da validação.

## Regra nº 1: quem roda a pipeline é o usuário

**Claude NÃO dispara scripts da pipeline** (`0a_*.py` até `8_*.py`, `rodar.py`). O usuário roda
tudo no terminal dele — a ideia é human-in-the-loop, com ele visualizando o progresso ao vivo.
O papel do Claude é: explicar o que esperar antes de cada etapa, ler código e ajudar a
debugar quando algo falha, inspecionar resultados depois (Bash/Python **read-only** é OK), e
implementar correções/features pontuais quando pedido. Ferramentas auxiliares de leitura/
diagnóstico (`ferramentas/`, inspeção de CSV) o Claude pode rodar livremente.

## Restrição crítica de custo de LLM

**Não há orçamento para o modelo caro** (`OPENAI_MODEL_PASS2`, hoje `qwen3.7-plus`). Use sempre
o modelo barato (`OPENAI_MODEL_PASS1`, `inclusionai/ling-2.6-flash`). Isso importa
especialmente na **etapa 6c**: ela só usa o modelo barato se rodar com a flag `--fraco` — sem
essa flag, cai no modelo caro por padrão. **Nunca sugerir rodar 6c sem `--fraco`.**

## "Regra dos 5" — foi removida intencionalmente

O README/GUIA ainda descrevem uma "regra dos 5" (mínimo/top-5 itens por código de catálogo).
Essa regra **foi desativada a pedido explícito do usuário**, via `.env`:

```
MIN_ITENS=1   # qualquer código fecha com só 1 item confirmado
TOP_N=0       # sem teto — traz TODAS as referências confirmadas não sinalizadas por código
```

Isso está confirmado e é comportamento esperado desde 2026-08. **Não tratar >5 itens por
código como bug ou anomalia** — já aconteceu de investigar isso à toa numa sessão anterior.

## Dependência externa: PDFs antigos ficaram para trás

~90% das linhas de dados herdadas do v2 (colunas `pasta_arquivos`) apontam para caminhos
**absolutos** em `C:\Users\diego\Documents\dev\plaseg\itens-via-script\itens-contratos-atas-v2\
data\arquivos\` (111 GB de PDFs brutos). Essa pasta **não foi movida** para este repo — ficou
intencionalmente no lugar antigo, porque:
- os caminhos absolutos continuam válidos enquanto ela não for movida/renomeada;
- os itens já processados por ela já têm o texto extraído em `data/5_pdf_texto.csv`/
  `5_itens_enriquecidos.csv` — não dependem mais do PDF bruto no dia a dia;
- mover 111 GB e reescrever caminhos em CSVs de centenas de MB era desnecessário e arriscado.

Só os ~30k itens coletados **depois** que a v3 começou usam a pasta própria `data/arquivos/`
(relativa, dentro deste repo). **Se um dia `itens-contratos-atas-v2` for movido/apagado**, os
PDFs antigos ficam inacessíveis para reprocessamento (mas os dados já extraídos continuam
intactos aqui).

## Padrão de atualização incremental (o que diferencia o v3)

O v3 existe para permitir `--atualizar`: reprocessar só o que mudou desde a última coleta, sem
refazer tudo do zero. Peças desse desenho, reaproveitáveis como padrão:

- **Watermark** (etapa 2): `data_atualizacao_pncp` é o campo real que a API do PNCP usa para
  ordenar (desc); ele muda quando um documento é atualizado. `data_publicacao_pncp` é imutável.
  Como o v2 nunca salvou o campo real, ele foi reconstruído de forma conservadora a partir de
  `max(data_publicacao_pncp)` por `(termo, tipo_doc)` — sempre ≤ ao watermark real, então nunca
  pula nada (só re-varre um pouco a mais). Isso foi feito **uma vez só**, via
  `ferramentas/semear_watermark_v2.py`; a partir da primeira `--atualizar` real o mecanismo
  normal já sobrescreve com datas reais. **Não precisa rodar de novo.**
- **Dedup por texto** (etapa 3): classifica só `(descricao, unidade)` únicos e propaga o rótulo
  para todos os `item_key` iguais — corta o volume de chamadas de LLM em ~5x. A etapa 5b **não**
  tem esse dedup (cada item chama o LLM individualmente).
- **Custo vs escopo**: etapas caras (3, 5b, 6b-GPU, 6c) só processam itens **novos**
  (resumíveis por chave). Etapas baratas de agregação (4, 7, 8) sempre recomputam o **corpus
  inteiro** (antigo + novo), porque resultados como "mais barato por código" exigem comparar
  itens novos contra os antigos.
- **Cache de embeddings** (etapa 6a): parquet chaveado por `sha1(texto)`; só texto
  novo/desconhecido vai à GPU. Reruns reaproveitam o cache e ficam bem mais rápidos.
- **Corte em streaming** (etapa 6a): o corte top-K + piso por código é aplicado **durante** a
  geração dos pares (numpy direto nas matrizes de score), nunca depois de materializar o
  produto cartesiano completo em um DataFrame — isso já causou um `MemoryError` real com ~33M
  linhas. Não reintroduzir um `aplicar_corte` pós-hoc que dependa de `groupby().rank()` sobre o
  DataFrame inteiro.
- **Snapshot/delta** (etapa 8, flag `--novos`): compara as chaves do export atual contra
  `data/8_export_snapshot.csv` (o snapshot do último `--novos`) para reportar só o que é novo,
  e sempre avança o snapshot no final. O export completo (sem `--novos`) nunca toca o snapshot.
  **Armadilha conhecida**: a primeira vez que `--novos` roda sem snapshot prévio, TUDO aparece
  como novo (mesmo padrão do watermark). Se isso confundir o usuário, a correção é semear o
  snapshot manualmente a partir do baseline que ele realmente quer usar como "já entregue"
  (ex.: as chaves do último export oficial), não tratar como bug.

## Estado da validação (atualizado em 2026-08-16)

Todo o pipeline (0a → 8) já foi validado end-to-end pelo usuário, rodando o fluxo
`--atualizar` script por script, reaproveitando os dados migrados do v2:

| Etapa | Status | Observação |
|---|---|---|
| 0a | ✅ | delta limpo (catálogo estável) |
| 1 | ✅ | formato "termos por item" já é o base, sem custo LLM extra |
| watermark artificial | ✅ | seed único via `semear_watermark_v2.py --com-extra`, não repetir |
| 2 (`--atualizar`) | ✅ | +173k itens; progress bar do resgate de pendentes corrigida |
| 3 (classificar) | ✅ | dedup por texto; bug do `--retry-erros` corrigido |
| 4 (cortar) | ✅ | sem LLM; "regra dos 5" já removida (ver acima) |
| 5a/5b (enriquecer PDF) | ✅ | caminho base (sem alt); ~30k itens, 0 erros |
| 6a (pares+embeddings) | ✅ | bug de `MemoryError` corrigido (corte em streaming) |
| 6b (reranker) | ✅ | GPU remota |
| 6c (LLM ambíguos) | ✅ | sempre com `--fraco` |
| 7 (agrupar) | ✅ | sem LLM, recomputa tudo |
| 8 (exportar) | ✅ | feature nova `--novos` (delta incremental) implementada e testada |

Não há mais nenhuma etapa pendente de validação — o próximo ciclo de trabalho é rodadas
normais de `--atualizar` no dia a dia, não mais validação do mecanismo em si.

## Onde ficam as coisas úteis para debugar

- `ferramentas/` — scripts de apoio pontuais (correção de schema, seed de watermark,
  calibração de thresholds). Não fazem parte do fluxo normal.
- `legado/` — patches/scripts aposentados (ex.: `2b_corrigir_precos_homologados.py`), mantidos
  só de referência.
- `data/checkpoints/` — estado de resumo por etapa (chaves já concluídas).
- `data/erros/` — falhas de registro por etapa, não derrubam a execução.
- `.env` — nunca commitar (está no `.gitignore`); tem chaves de API e a URL do túnel ngrok da
  GPU remota (`GPU_BASE_URL`), que muda de tempos em tempos.
