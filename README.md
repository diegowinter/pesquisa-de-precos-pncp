# Pipeline `itens-contratos-atas` v3

Pesquisa de preços de itens de segurança pública via PNCP: parte do catálogo CATMAT/CATSER
filtrado por allow-list, coleta contratos/atas do PNCP por conceito, e afunila com um funil
de custo crescente (classificação → corte → rejeitor híbrido → reranker local → LLM só no
ambíguo) até os **5 itens confirmados mais baratos** por código de catálogo.

O desenho completo (regras de negócio, formatos de arquivo, convenções) está em
[`GUIA_IMPLEMENTACAO_PIPELINE.md`](GUIA_IMPLEMENTACAO_PIPELINE.md). O projeto de
transformação desta pipeline em aplicação (banco, API, web) está em [`docs/`](docs/).

> **A "regra dos 5" descrita abaixo está DESATIVADA** (`MIN_ITENS=1`, `TOP_N=0` no `.env`).
> Mais de 5 itens por código é comportamento esperado. Ver [`CLAUDE.md`](CLAUDE.md) e
> [ADR-016](docs/07_DECISOES.md#adr-016).

> **v3 vs v2** — o v3 nasce como cópia do v2 (scripts + resultados + checkpoints), **sem**
> `data/arquivos/` (os PDFs são re-baixados sob demanda pela etapa 2). Os patches retroativos
> (ex.: `2b_corrigir_precos_homologados.py`) foram **aposentados**: o caminho base já coleta o
> valor homologado inline, então há um caminho canônico só. Desde a Fase 0 o patch vive na tag
> git `legado-2b-precos-homologados`, não mais em `legado/`. O v2 permanece intacto como
> fallback.

## Fluxo

```
0a  Catálogo + filtro allow-list (PDM / codigoServico)     → data/0a_catalogo_filtrado.csv
1   Conceitos + termos de busca (LLM, versionado à mão)     → data/1_conceitos_termos.csv
2   Coleta larga PNCP (busca → homologado → PDF → explode)   → data/2_itens_coletados.csv
3   Classificação de categoria por item (LLM local)          → data/3_itens_classificados.csv
4   Corte antecipado (regra dos 5, categorias < 5)           → data/4_itens_sobreviventes.csv
5   Enriquecimento via PDF (parse/OCR → extração → âncora)   → data/5_itens_enriquecidos.csv
6a  Pares (catálogo × item, mesma categoria) + rejeitor      → data/6a_pares_candidatos.csv
6b  Reranker local (aceito / rejeitado / ambíguo)            → data/6b_pares_rerankeados.csv
6c  LLM forte só nos ambíguos + acúmulo de rótulos           → data/6c_pares_validados.csv
7   Agrupar por código, sanity de preço, top 5              → data/7_itens_agrupados.csv
8   Export XLSX Plaseg                                       → data/8_itens_plaseg.xlsx
```

**Regra dos 5**: cada código de catálogo precisa de 5 itens confirmados; ficam os 5 mais
baratos por preço unitário. Pares nunca são deduplicados (item ambíguo é julgado em cada
categoria). O corte da etapa 4 é a versão "matematicamente segura"; a contagem definitiva é
na etapa 7, sobre confirmados e fora os outliers de preço.

## Tabela etapa → entrada → saída

Cada etapa é um módulo em `pesquisa_precos/etapas/`, executado com
`python -m pesquisa_precos.etapas.<módulo>`.

| Módulo | Entrada | Saída | LLM/GPU |
|---|---|---|---|
| `e0a_catalogo` | Dados Abertos Compras.gov | `0a_catalogo_*` | — |
| `e1_termos` | `0a_catalogo_filtrado.csv` | `1_conceitos_termos.csv` | LLM |
| `e2_coletar` | `1_conceitos_termos.csv` | `2_itens_coletados.csv` + PDFs | — |
| `e3_classificar` | `2_itens_coletados.csv` | `3_itens_classificados.csv` | LLM local |
| `e4_cortar` | `2_*`, `3_*` | `4_itens_sobreviventes.csv` | — |
| `e5a_ocr` | `4_*`, PDFs | `5_pdf_texto.csv` | OCR |
| `e5b_extrair` | `4_*`, `5_pdf_texto.csv` | `5_itens_enriquecidos.csv`, `5_itens_destino.csv` | LLM |
| `e6a_pares` | `4_*`, `5_*`, `0a_*`, `1_*` | `6a_pares_candidatos.csv` | GPU (embedder) |
| `e6b_rerank` | `6a_*` | `6b_pares_rerankeados.csv` | GPU (reranker) |
| `e6c_validar` | `6b_*` | `6c_pares_validados.csv`, `6_rotulos_acumulados.csv` | LLM |
| `e7_agrupar` | `6b_*`, `6c_*`, `4_*`, `5_*`, `0a_*` | `7_itens_agrupados.csv` | — |
| `e8_exportar` | `7_*` | `8_itens_plaseg.xlsx` | — |

Caminho alternativo da etapa 5 (`rodar.py --caminho-5 alt`): `e5_alt_a_tabela` extrai a tabela
do PDF por modelo de visão e `e5_alt_b_casar` casa cada item da API contra ela.

## Convenções

- Saídas prefixadas pela etapa que as produziu (`data/{N}{letra?}_*`). Checkpoints em
  `data/checkpoints/{N}_*`; erros em `data/erros/{N}_erros.csv`. **Os caminhos não são
  escritos à mão em lugar nenhum**: todos vivem em `pesquisa_precos/config/paths.py`.
- Toda etapa que itera é **resumível**: relê as chaves já concluídas da própria saída e as
  pula; falhas de registro vão para o log de erros sem derrubar a execução.
- Todo I/O de texto é utf-8 explícito (defesa contra o bug de acentos cp1252 no Windows).
- **GPU (6 GB)**: embedder, reranker, OCR e LLM local **nunca rodam simultaneamente**. As
  etapas são sequenciais e cada uma carrega seu modelo no início e libera ao final.

## Configuração

Copie `.env.example` para `.env` e preencha (OpenRouter, LM Studio, OCR, modelos e
thresholds — ver seção 1.5 do guia). Instale o pacote em modo editável:

```
uv sync                 # ou:  pip install -e ".[dev]"
```

As dependências vivem no `pyproject.toml` (o `requirements.txt` ficou obsoleto na Fase 0).
`sentence-transformers` e `rank-bm25` só são necessárias para as etapas 6a/6b; `pymupdf` para
a 5.

Rodar uma etapa isolada:

```
python -m pesquisa_precos.etapas.e3_classificar --provedor local --concurrency 8
python -m pesquisa_precos.etapas.e8_exportar --novos
```

## Orquestração

`rodar.py` encadeia as etapas, para no primeiro erro e reporta:

```
python rodar.py --completo   [--provedor openrouter] [--remoto] [--caminho-5 base|alt]
python rodar.py --atualizar  [--provedor openrouter] [--remoto] [--sem-catalogo]
python rodar.py --atualizar --de 3          # retoma a partir de uma etapa
python rodar.py --completo  --dry-run        # só imprime a sequência
```

No `--atualizar`: a 0a rebaixa o catálogo (detecta PDMs novos/removidos → delta) e a 2 roda com
`--atualizar` (para no watermark + revisita pendentes); as demais são resumíveis/agregadoras e só
tocam o novo. O desenho do incremental está em [`CLAUDE.md`](CLAUDE.md).

## Utilitários

- `limpar.py --etapa N` — apaga saídas/checkpoints da etapa N em diante (preserva os ativos
  caros: `0a_*`, `1_conceitos_termos.csv`, `6_rotulos_acumulados.csv`). Também `--arquivos`
  (PDFs) e `--tudo`.
- `ferramentas/calibrar_thresholds.py --amostrar | --analisar` — prepara a amostra rotulável
  e sugere `REJEITOR_THRESHOLD`, `RERANK_T_ACEITA`, `RERANK_T_REJEITA` a partir dela.
- `pytest` — guarda estrutural: confere que os caminhos das etapas continuam apontando para
  `data/` e que o pacote inteiro importa.

Todas as etapas de LLM aceitam `--provedor local|openrouter` e a maioria um `--limite N` para
validação barata.

## Legado

O código da v1 fica em
[`../itens-via-script/itens-contratos-atas/`](../itens-via-script/itens-contratos-atas/) e
dados antigos em [`data/legado/`](data/legado/). Os 111 GB de PDFs herdados continuam em
`../itens-via-script/itens-contratos-atas-v2/data/arquivos/` — ver [`CLAUDE.md`](CLAUDE.md). A curadoria do catálogo por LLM (antiga etapa 0b) foi
**aposentada** e substituída pela allow-list em `pesquisa_precos/core/catalogo/local.py`
(`PDMS_MATERIAIS`, `CODIGOS_SERVICOS`).
