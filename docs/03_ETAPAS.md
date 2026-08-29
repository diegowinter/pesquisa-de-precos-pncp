# 03 — Contrato de etapa e especificação

## 1. O contrato

Toda etapa é um módulo em `etapas/` que expõe **exatamente** esta interface. Web, API e runner
chamam a mesma função — não existe caminho alternativo.

```python
# etapas/base.py
from typing import Protocol
from pydantic import BaseModel

class ContextoExecucao(Protocol):
    """Tudo que a etapa precisa do mundo externo. Injetado pelo runner."""
    run_id: int
    run_etapa_id: int
    acao: Literal["atualizar", "retomar", "refazer"]
    modo: Literal["assistido", "sequencial", "amostra", "simulacao"]
    db: Session
    provedores: Provedores          # .chat / .embed / .rerank / .ocr
    config: ConfigResolvida         # valores da config_versao do run

    def progresso(self, processados: int, total: int | None = None) -> None: ...
    def log(self, nivel: str, msg: str, **contexto) -> None: ...
    def erro_item(self, chave: str, exc: Exception) -> None: ...
    def cancelado(self) -> bool: ...      # checar em todo laço
    def gastar(self, usd: float) -> None: ...  # levanta TetoDeCustoExcedido

class ResultadoEtapa(BaseModel):
    processados: int
    erros: int
    metricas: dict          # vai para run_etapa.metricas, exibido na UI
    preview: list[dict]     # amostra p/ o gate (máx. 50 linhas)
```

Cada módulo de etapa define:

```python
class Params(BaseModel):
    """Schema Pydantic — fonte única de: validação, formulário da web, body da
    API e documentação. (Gerava também as flags do CLI, até a Fase 13.)"""
    ...

def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Quantas unidades, quantas chamadas de LLM, quanto custa, quanto demora.
    NÃO pode chamar provedor pago. É o que alimenta o gate e o modo 'simulacao'."""

def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    """Executa a etapa inteira. Idempotente sob retomada."""
```

### 1.1 Regras invioláveis da implementação

1. **Resultado + avanço de estado no mesmo commit.** Nunca gravar resultado e marcar como feito
   em transações separadas — a retomada pagaria o LLM duas vezes.
2. **Checar `ctx.cancelado()` em todo laço externo.** Cancelamento não pode depender de matar o
   processo à força.
3. **`ctx.progresso()` a cada lote**, não a cada item (evita escrita excessiva no banco).
   Sugestão: a cada 1s ou a cada 100 unidades, o que vier primeiro.
4. **Erro de unidade não derruba a etapa.** Vai para `erro_item` e o laço continue.
   Só erro de infraestrutura (banco fora, provedor inacessível) aborta.
5. **`estimar()` nunca gasta.** Se precisar de amostra, usa dados já no banco.
6. **Nenhuma etapa lê `.env` diretamente.** Tudo vem de `ctx.config` e `ctx.provedores`.
7. **Nenhuma etapa toca em disco** (ADR-018/ADR-020). Não importa `config/paths.py`, não expõe
   `Path`, não lê nem escreve arquivo — o banco é o único meio de persistência.

## 2. O registry

`etapas/registry.py` é a fonte única da ordem, das dependências e dos metadados. Runner, web e
API descobrem etapas por aqui — **ninguém hardcoda a sequência `0a → 8`**.

```python
@dataclass(frozen=True)
class DefinicaoEtapa:
    chave: str                      # '0a', '1', '2', ... '8'
    titulo: str
    modulo: str                     # 'steps.e2_collect'
    depende_de: tuple[str, ...]
    custo: Literal["gratis", "cpu", "gpu", "pago"]
    precisa_gate: bool              # padrão do modo assistido
    recomputa_corpus: bool          # True = sempre recalcula tudo, não só o novo
    params_model: type[BaseModel]
    versao_codigo: str              # bump manual ao mudar a lógica → muda o fingerprint
```

### 2.1 Tabela do registry

| Chave | Título | Depende de | Custo | Gate | Recomputa corpus |
|---|---|---|---|---|---|
| `0a` | Obter catálogo CATMAT/CATSER | — | grátis | não | sim |
| `1` | Gerar termos de busca | `0a` | **pago** | **sim** | não |
| `2` | Coletar no PNCP | `1` | grátis (HTTP) | **sim** | não |
| `3` | Classificar itens | `2` | **pago** | **sim** | não |
| `4` | Cortar / definir escopo | `3` | grátis | **sim** | **sim** |
| `5` | Extrair e enriquecer | `4` | **pago** + gpu | não | não |
| `6a` | Gerar pares + rejeitor | `4`, `5` | gpu | não | **sim** |
| `6b` | Rerankear pares | `6a` | gpu | não | não |
| `6c` | Validar ambíguos (LLM) | `6b` | **pago** | **sim** | não |
| `7` | Agrupar e ranquear | `6c` | grátis | não | **sim** |
| `8` | Exportar XLSX | `7` | grátis | não | **sim** |

**`recomputa_corpus`** distingue as etapas baratas de agregação (que precisam comparar itens
novos contra os antigos — "mais barato por código" exige o corpus inteiro) das caras
(que só processam o inédito). É a regra que sustenta o custo do modo `atualizar`.

## 3. Camadas de resolução de parâmetros

Precedência crescente. Resolvida uma vez, no início da etapa, e gravada em
`run_etapa.params_efetivos`.

1. **Default do schema Pydantic** (no código)
2. **`config_valor`** da `config_versao` do run (editável pela interface)
3. **`run_etapa.params_override`** (o que o usuário escolheu ou editou no gate)

> **Retomada usa `params_efetivos` gravado, nunca recalcula.** Um run retomado não pode mudar de
> comportamento porque alguém mexeu na config no meio.

### 3.1 Categorias de parâmetro

| Tipo | Exemplo | Onde vive |
|---|---|---|
| Domínio | `min_itens`, `top_n`, thresholds, faixas de preço | `config_valor`, versionado |
| Execução | ação (`atualizar`/`retomar`/`refazer`), `limite` | override do run |
| Custo/modelo | provedor, modelo, teto de USD | **política**, não flag livre |
| Debug | `dry_run`, `limite_docs` | `Params`, discreto no formulário |

## 4. Especificação por etapa

Cada bloco descreve: o que a etapa faz, entradas/saídas no **schema novo**, parâmetros, o que
muda em relação ao script atual, e as armadilhas conhecidas.

---

### 0a — Obter catálogo

**Origem:** [`etapas/e0a_catalogo.py`](../pesquisa_precos/etapas/e0a_catalogo.py)

Baixa CATMAT (materiais) e CATSER (serviços) da API de Dados Abertos do Compras.gov.br.

- **Escreve:** `catalogo_item`, `catalogo_snapshot`
- **Params:** `tipo: material|servico|ambos = ambos`, `so_grupos_seguranca: bool = True`, `forcar: bool = False`
- **Delta:** compara `hash_linha` contra o snapshot anterior → códigos `adicionado` / `removido` /
  `alterado`. Código removido vira `ativo=false` (nunca DELETE) e é podado na etapa 8.
- **Resumível:** por página da API.

---

### 1 — Gerar termos de busca `[GATE]`

**Origem:** [`etapas/e1_termos.py`](../pesquisa_precos/etapas/e1_termos.py)

Para cada item do catálogo, o LLM gera termos de busca **genéricos** (a partir de `nome_pdm` +
`descricao`) e uma categoria. Depois expande variações de grafia e agrega um termo por linha,
unindo os códigos que o pediram.

- **Lê:** `catalogo_item` (ativos)
- **Escreve:** `termo`, `termo_codigo`, `catalogo_item.categoria`
- **Params:** `concurrency: int = 8`, `regerar: bool = False`, `limite: int | None`
- **Custo:** ~2.212 chamadas (uma por código). Cacheável por `(codigo, prompt_versao)`.

**Este é o gate de maior alavancagem do sistema.** Um termo ruim contamina tudo abaixo e o
usuário só descobre horas depois. O gate deve oferecer:

- lista de termos com os códigos que cada um atende;
- **botão "testar"** — consulta a API do PNCP com o termo e retorna a contagem de resultados
  *antes* de aceitar (feature independente, boa por si só);
- ativar/desativar termo individualmente (`termo.ativo`), gravando quem e quando.

**Armadilha:** categoria nunca pode ficar vazia. Cascata atual: LLM → maioria dentro do mesmo
`codigo_pdm` → mapa `nome_grupo` → `"outros"`. Preservar.

---

### 2 — Coletar no PNCP `[GATE: volume]`

**Origem:** [`etapas/e2_collect.py`](../pesquisa_precos/etapas/e2_collect.py), `core/coleta/collect_pncp.py`, `core/coleta/search_pncp.py`

Para cada `(termo ativo, tipo_doc)`: busca paginada, dedup por `numero_controle_pncp`, e para
documento novo consulta os itens homologados da API.

- **Lê:** `termo`, `coleta_watermark`
- **Escreve:** `documento`, `documento_termo`, `item`, `coleta_watermark`
- **Params:** `tipos_doc: list = [contrato, ata]`, `ignorar_cache: bool = False`, `limite_termos: int | None`
- **Resumível:** por `(termo_id, tipo_doc)`

**Mudança importante em relação ao script atual: esta etapa NÃO baixa mais PDF.** Ela obtém só a
"capa" (metadados + itens da API). O download passa para a etapa 5, depois do corte. Consequências:

- a etapa fica muito mais rápida e barata (só HTTP);
- o dedup global por texto da etapa 3 é preservado (era o risco do desenho streaming);
- **não se baixa PDF de documento que vai ser descartado** — hoje isso acontece.

**Watermark:** `data_atualizacao_pncp` é o campo real que a API usa para ordenar (desc) e muda
quando o documento é atualizado. `data` (publicação) é imutável e **não** vem ordenada. Parar de
paginar ao cruzar o watermark é seguro. O watermark do acervo v2 já foi semeado de forma
conservadora — não semear de novo.

**Gate:** mostra volume descoberto (documentos, itens, textos únicos) e a estimativa de custo da
etapa 3. É o número que o usuário precisa antes de liberar a primeira etapa paga do ciclo.

**Armadilha:** um documento é encontrado por vários termos. O dedup grava o documento uma vez e
acrescenta linhas em `documento_termo` — nunca reprocessa o documento.

---

### 3 — Classificar itens `[GATE]`

**Origem:** [`etapas/e3_classify.py`](../pesquisa_precos/etapas/e3_classify.py)

Classificação multi-label de categoria, **por texto único**, não por item.

- **Lê:** `item` (via `texto_hash` distintos ainda não em `texto_classificacao`)
- **Escreve:** `texto_classificacao`, `item_categoria`
- **Params:** `concurrency: int = 8`, `limite: int | None`, `retry_erros: bool = False`
- **Custo:** ~320k textos únicos vs. 1,6M itens — **dedup de ~5x**. Não perder isso.

**O dedup agora é permanente**, não intra-execução: `texto_classificacao` é consultada antes de
qualquer chamada, então textos já vistos em runs anteriores custam zero. O ganho melhora com o
tempo.

Item sem nenhuma categoria de conteúdo morre aqui — a "portaria de nomeação" nunca mais custa
nada nas etapas seguintes.

**Gate:** custo estimado = `textos_únicos_novos × custo_por_chamada`. Este número tem que
aparecer antes do play.

---

### 4 — Cortar / definir escopo `[GATE]`

**Origem:** [`etapas/e4_cut.py`](../pesquisa_precos/etapas/e4_cut.py)

Sem LLM. Marca `item.sobrevivente = true` para item com ≥1 categoria e atualiza
`documento.n_itens_sobreviventes` e `documento.estado` (`fora_de_escopo` quando zero).

- **`recomputa_corpus = True`** — sempre recalcula o corpus inteiro.
- **Params:** nenhum relevante.

**A "regra dos 5" foi removida.** O único filtro é "item tem ≥1 categoria". A contagem por
categoria é **apenas diagnóstico**. Não reintroduzir descarte por `MIN_ITENS` aqui.

**Gate:** mostra quantos documentos e itens entram na etapa 5, com a estimativa de custo por
estratégia de roteamento. É o último ponto barato antes de gastar com download e LLM.

---

### 5 — Extrair a tabela do documento e enriquecer os itens

**Módulo:** [`steps/e5_extract.py`](../pesquisa_precos/steps/e5_extract.py) ·
regras em [`core/extraction.py`](../pesquisa_precos/core/extraction.py)

Duas chamadas de LLM por documento ([ADR-023](07_DECISOES.md#adr-023)). Fluxo:

```
baixa o PDF do PNCP  → manda o ARQUIVO INTEIRO como anexo (capacidade `extract`)
                     → recebe a TABELA DE ITENS em texto, "as it is"
                     → DESCARTA o PDF (ADR-012)
                     → grava documento_extracao.tabela_texto
                     → por item da API: casa contra ESSA tabela (capacidade `chat`)
                     → grava item_enriquecido (contrato de saída)
```

- **Lê:** `item` (sobreviventes), `documento`
- **Escreve:** `documento_extracao`, `item_enriquecido`, `documento.estado`
- **Capacidades:** `("extract", "chat")` — modelos diferentes de propósito, ver ADR-023
- **Params:** `concurrency_docs: int = 4`, `concurrency_llm: int = 8`, `max_mb: int = 32`,
  `file_parser: bool = True`, `limite_docs: int | None`, `documentos: str | None`
- **Chave de resumo:** `documento.estado = 'extraido'` (ADR-018). Reprocessar um documento
  sobrescreve o veredito de TODOS os seus itens.

#### 5.1 A tabela é texto livre, não um esquema

O modelo devolve a tabela **como ela é no documento**: um traz fornecedor e modelo, outro só
descrição/quantidade/preço. Impor colunas fixas obrigaria o modelo a preencher campo
inexistente — convite para inventar. `documento_extracao.tabela_texto` guarda a resposta como
veio; quem estrutura é a segunda chamada, item a item.

Se o documento não tiver tabela de itens, a resposta combinada é `SEM_TABELA`, e todos os seus
itens saem `sem_texto` / `doc_status = ilegivel`. A linha em `documento_extracao` é gravada
mesmo assim: é o registro de que o documento já foi tentado, e é o que impede repagar o
download na execução seguinte.

#### 5.2 Validação (preservar integralmente)

- confirma o item pela **quantidade** (tolerância `max(1.0, 1%)`) **ou** por match exato de
  preço acima de `PRECO_FINGERPRINT = 1000.0`;
- confirmado o item, o preço deixa de ser critério de aceite e vira **saída**: a API traz o
  estimado, o PDF traz o homologado/registrado. Divergência é **sinalizada, não descartada**;
- banda de sanidade `0,3×…3,0×` marca provável misparse de número BR.

**Detector de PDF trocado:** `doc_status` é derivado do documento inteiro — nenhum item
confirmou = `suspeito`; nenhuma tabela saiu do documento = `ilegivel`. Daí sai o `destino`:
`manter` (confirmado) / `revisar` (doc suspeito ou ilegível) / `descartar` (falha isolada em
documento saudável).

#### 5.3 Circuit breaker

Vinte extrações seguidas falhando **sem nenhuma ter dado certo** abortam a etapa: o problema
é do provedor (modelo que não aceita anexo, chave vencida), não dos documentos. Sem isso a
etapa desce a fila inteira produzindo falha em série — foi o que aconteceu na etapa 3 em
2026-08-25, com um modelo aposentado no OpenRouter.

Documento **sem tabela** não conta como falha nem como sucesso para o breaker: ele foi lido, a
resposta chegou, e o veredito "não há tabela aqui" é um resultado legítimo.

#### 5.4 O que NÃO fazer

- persistir o PDF além do documento — sempre `try/finally` + `shutil.rmtree` (ADR-012);
- usar o preço como critério de aceite (é SAÍDA, não filtro — [08_CONVENCOES.md §5.9](08_CONVENCOES.md));
- mandar o documento inteiro para a chamada de casamento: o ponto das duas passadas é que a
  segunda vê **só a tabela**;
- reintroduzir estratégias, roteamento ou escalonamento. Ver ADR-023 para o que isso custou.

---

### 6a — Gerar pares + rejeitor híbrido

**Origem:** [`etapas/e6a_pairs.py`](../pesquisa_precos/etapas/e6a_pairs.py)

Produto (código × item) **restrito à mesma categoria**. Item multi-label pareia em todas as suas
categorias. **Sem dedup de pares** — é regra de negócio.

Score léxico (BM25 normalizado por categoria) + score semântico (cosseno bge-m3).
Sobrevive se `max(bm25_norm, cosseno) >= rejeitor_threshold` — basta um sinal dizer "pode ser".

- **Lê:** `item` (sobreviventes), `item_categoria`, `item_enriquecido`, `catalogo_item`
- **Escreve:** `par`, `embedding_cache`
- **`recomputa_corpus = True`**
- **Params:** `rejeitor_threshold: float = 0.30`, `sem_embedding: bool = False`, `top_k: int`, `piso_por_codigo: int`

**⚠ CORTE EM STREAMING — NÃO REGREDIR.** O corte top-K + piso por código é aplicado **durante** a
geração dos pares (numpy direto nas matrizes de score), nunca depois de materializar o produto
cartesiano num DataFrame. Um `aplicar_corte` pós-hoc com `groupby().rank()` sobre o DataFrame
inteiro já causou `MemoryError` real com ~33M linhas. O arquivo
`data/6a_pares_candidatos_PRECORTE.csv` (3,7 GB) é o fóssil desse bug.

**Cache de embeddings:** chaveado por `(texto_hash, provedor, modelo, dimensao)`. Só texto
novo vai à GPU. BM25 e corte são recomputados frescos (baratos, CPU).

---

### 6b — Rerankear pares

**Origem:** [`etapas/e6b_rerank.py`](../pesquisa_precos/etapas/e6b_rerank.py)

Cross-encoder sobre `(texto_catalogo, descricao_final)`. **Custo zero de token** — roda na GPU.

```
score >= rerank_t_aceita  → aceito
score <= rerank_t_rejeita → rejeitado
entre                     → ambiguo   (só estes vão para a 6c)
```

- **Lê/escreve:** `par` (só os `sobreviveu = true`)
- **Params:** `rerank_t_aceita: float = 0.80`, `rerank_t_rejeita: float = 0.30`, `limite: int | None`
- **Resumível:** por `par_key`

**Trocar o modelo de reranker exige recalibrar os thresholds.** A base para isso é a tabela
`rotulo` (250k linhas acumuladas) — ver `tools/calibrate_thresholds.py`.

---

### 6c — Validar ambíguos com LLM `[GATE]`

**Origem:** [`etapas/e6c_validate.py`](../pesquisa_precos/etapas/e6c_validate.py)

Só a faixa `ambiguo` chega aqui — tipicamente a minoria (57k de 250k no acervo atual).

- **Lê:** `par` onde `decisao = 'ambiguo'`
- **Escreve:** `par.veredito`, `par.justificativa`, `par.decisao_final`, `rotulo`
- **Params:** `concurrency: int = 8`, `limite: int | None`
- **Resumível:** por `par_key`

**⚠ RESTRIÇÃO DE CUSTO Nº 1.** O script atual usa o modelo caro (`OPENAI_MODEL_PASS2`) por padrão
e só usa o barato com a flag `--fraco`. **Isso inverte no projeto novo:** o modelo barato é o
único permitido por padrão; usar o caro exige configuração explícita **com teto de gasto**.
Comportamento seguro não pode depender de alguém lembrar de digitar uma flag.

**Acúmulo de rótulos:** toda decisão final — aceites/rejeições da 6b por threshold extremo **e**
vereditos da 6c — é appendada em `rotulo`. Essa tabela é o ativo de calibração e futuro
fine-tuning. Nunca truncar.

---

### 7 — Agrupar e ranquear

**Origem:** [`etapas/e7_group.py`](../pesquisa_precos/etapas/e7_group.py)

Confirmados = `decisao='aceito'` ∪ `veredito='sim'`. Antes do ranking:

1. **Outlier de preço por IQR, por código:** `< Q1 − 3×IQR` ou `> Q3 + 3×IQR` → `flag_preco=true`.
   Fica no resultado mas fora do ranking — um erro de unidade não pode contaminar a pesquisa.
2. **Faixas de preço por categoria** (`faixa_preco`), quando configuradas.
3. Ranking por preço unitário crescente.

- **Escreve:** `grupo_item`
- **`recomputa_corpus = True`**
- **Params:** `min_itens: int = 1`, `top_n: int = 0`, `fator_iqr: float = 3.0`

**⚠ `min_itens=1` e `top_n=0` são os valores corretos e intencionais.** `top_n=0` significa
**sem teto** — traz todas as referências confirmadas não sinalizadas por código. Mais de 5 itens
por código **não é bug**. Isso já foi investigado à toa em uma sessão anterior.

---

### 8 — Exportar XLSX PLASEG

**Origem:** [`etapas/e8_export.py`](../pesquisa_precos/etapas/e8_export.py)

Aba "Itens PLASEG", schema fechado com o cliente:

| Coluna | Origem |
|---|---|
| Código CATMAT/CATSER | `catalogo_item.codigo` |
| Material/Serviço | `catalogo_item.tipo` |
| Nome | descrição CATMAT **antes da 1ª vírgula** (o núcleo, sem características) |
| Descrição Base | `catalogo_item.descricao` completa |
| Descrição Específica | `item_enriquecido.descricao_final` (PDF quando houver, senão API) |
| Origem | "Ata" ou "Contrato" |
| Fim de Vigência | Ata → data final; Contrato → assinatura + 1 ano (calculada) |
| + params de rastreio | órgão, CNPJ, UF, nº controle, ano, item, unidade, quantidade, valor homologado, valor estimado, fornecedor, data do resultado |

- **Escreve:** `export`, `export_snapshot` (só no modo `novos`)
- **`recomputa_corpus = True`**
- **Params:** `modo: completo|novos = completo`, `formato: xlsx|csv|ambos = ambos`

**Poda incremental:** códigos com `catalogo_item.ativo = false` são descartados do export.

**Delta (`novos`):** compara as chaves do export atual contra `export_snapshot` e reporta só o
novo, avançando o snapshot ao final. O export completo **nunca** toca o snapshot.

**Armadilha conhecida:** a primeira execução de `novos` sem snapshot prévio marca **tudo** como
novo. A correção é semear o snapshot a partir do último export oficial — não tratar como bug.

## 5. Estrutura de um módulo de etapa (modelo)

```python
"""Etapa 3 — classificação multi-label por texto único."""
from etapas.base import ContextoExecucao, ResultadoEtapa, Estimativa
from pydantic import BaseModel, Field

CHAVE = "3"
VERSAO_CODIGO = "3.0.0"        # bump ao mudar a lógica → invalida fingerprint

class Params(BaseModel):
    concurrency: int = Field(8, ge=1, le=32, description="Chamadas simultâneas ao LLM")
    limite: int | None = Field(None, description="Teto de textos (debug)")
    retry_erros: bool = Field(False, description="Reprocessa erro_item pendentes")

def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    n = repo.contar_textos_nao_classificados(ctx.db)
    preco = ctx.provedores.chat.custo_estimado(tokens_in=800, tokens_out=120)
    return Estimativa(unidades=n, chamadas_llm=n, custo_usd=n * preco,
                      duracao_s=n / (params.concurrency * 2))

def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    pendentes = repo.textos_nao_classificados(ctx.db, limite=params.limite)
    ctx.progresso(0, len(pendentes))
    feitos = erros = 0
    for lote in em_lotes(pendentes, 64):
        if ctx.cancelado():
            break
        for texto, resultado, erro in classificar_lote(lote, ctx):
            with ctx.db.begin():                    # resultado + estado no MESMO commit
                if erro:
                    ctx.erro_item(texto.hash, erro); erros += 1
                else:
                    repo.gravar_classificacao(ctx.db, texto, resultado); feitos += 1
        ctx.progresso(feitos + erros)
    repo.recomputar_item_categoria(ctx.db)          # derivada, SQL puro
    return ResultadoEtapa(
        processados=feitos, erros=erros,
        metricas={"textos_unicos": feitos, "itens_afetados": repo.contar_itens_classificados(ctx.db)},
        preview=repo.amostra_classificacao(ctx.db, 30),
    )
```
