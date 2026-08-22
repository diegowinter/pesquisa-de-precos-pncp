# 01 — Arquitetura

## 1. O que o sistema é

Uma aplicação **monolítica** Python que executa a pesquisa de preços PLASEG sobre o PNCP, com
banco relacional, API HTTP e interface web servida pela própria aplicação.

O eixo do desenho é uma constatação sobre o problema: **a execução é única**. Não existem
múltiplos usuários rodando pesquisas diferentes em paralelo. Vários usuários podem *observar* a
mesma execução, mas o trabalho é serial e centralizado. Toda a arquitetura deriva disso.

### O que o sistema NÃO é

Isto está aqui para evitar que um agente "melhore" o projeto na direção errada:

| Não é | Por quê |
|---|---|
| Multi-tenant | Execução única, usuário único operando. Não modele `tenant_id`. |
| Distribuído / microserviços | Um processo, um banco, um servidor. |
| Baseado em fila de mensagens (Celery, RabbitMQ, Redis) | Sem concorrência entre runs; o banco é a fila. |
| Containerizado por padrão | `systemd` + `git pull` basta. Docker é opcional, não requisito. |
| Automático | A pipeline **não avança sozinha**. Quem dá play é o usuário. |
| Um front-end separado | HTML servido pelo próprio app. Ver [ADR-003](07_DECISOES.md#adr-003). |

## 2. Diagrama de blocos

```
┌──────────────────────────────────────────────────────────────────────┐
│  Processo web (FastAPI)                — leve, nunca trabalha pesado │
│                                                                      │
│   web/  (Jinja2 + HTMX)   ──┐                                        │
│   api/  (JSON)            ──┼──►  services/  ──►  db/ (repositórios) │
│                             │                                        │
│   POST /etapas/{id}/executar ─── grava intenção ──► tabela run_etapa  │
└──────────────────────────────────────────────────────────────────────┘
                                        │
                             spawn (subprocess)
                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Processo de execução (um por etapa, efêmero)                        │
│                                                                      │
│   runner/  ──►  etapas/e5b_extrair.py::executar(params, on_progress) │
│                    │                                                 │
│                    ├──► core/        (regras puras, testáveis)       │
│                    ├──► providers/   (chat / embed / rerank / ocr)   │
│                    └──► db/          (escreve resultado + heartbeat) │
│                                                                      │
│   Termina a etapa → grava status='concluida' → PROCESSO MORRE        │
└──────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                          ┌──────────────────────────┐
                          │   PostgreSQL (único)     │
                          │   estado + resultado +   │
                          │   config + custo + log   │
                          └──────────────────────────┘
```

**Invariante central:** o processo web **nunca** executa trabalho pesado. Ele grava intenção no
banco e dispara um subprocesso. Isso evita que pandas/embeddings/OCR travem a API pelo GIL.

## 3. Princípios estruturais

### 3.1 O banco é a única fonte de verdade
Não existe estado de execução em memória entre requisições. Se o servidor reiniciar no meio de
qualquer coisa, o estado no banco continua correto e o trabalho retoma do ponto em que parou.
Isso elimina a categoria inteira de bugs de "estado perdido".

Consequência prática: **aprovação de etapa é um `UPDATE`**, não um processo esperando.

### 3.2 A etapa é a unidade de execução
O usuário dá play em **uma etapa**. Ela roda até o fim (ou até falhar/ser cancelada), grava o
resultado, marca-se concluída e o processo morre. Ninguém avança para a próxima automaticamente.

O estado por item ainda existe — mas apenas como **mecanismo de retomada dentro da etapa**
(crash no meio da 6a repete só o que faltou). Não é mecanismo de fluxo.

### 3.3 Etapa como interface, implementação plugável
Cada etapa expõe:

```python
def executar(params: ParamsDaEtapa, ctx: ContextoExecucao) -> ResultadoEtapa: ...
```

A web e o runner chamam **a mesma função**. Ver [03_ETAPAS.md](03_ETAPAS.md).

> Até a Fase 13 havia também uma CLI, que chamava essa mesma função — a frase era "não existe
> lógica duplicada entre o jeito CLI e o jeito servidor". Ela saiu (ADR-020): a superfície é
> uma só, e `runner/processo.py` é o único ponto de entrada de execução.

Uma etapa pode ter **mais de uma implementação** com a mesma saída — é o caso da etapa 5
(estratégias `janela`, `completa`, `visao`). Quem consome a saída não sabe qual rodou.

### 3.4 Custo é cidadão de primeira classe
Toda chamada a um provedor pago é registrada em `llm_chamada` com tokens e custo estimado.
A partir daí: estimativa antes de executar, teto por run, dashboard por etapa, e a possibilidade
de recalibrar decisões (como o roteamento da etapa 5) com dado real em vez de palpite.

### 3.5 Reprocessar é perda
Cada item classificado, cada embedding, cada texto extraído é ativo permanente. Caches são
chaveados **por conteúdo** (`sha1` do texto + modelo + versão do prompt), então sobrevivem a um
"refazer". Nada caro é apagado — é invalidado logicamente.

## 4. Modelo de execução

### 4.1 Três semânticas de (re)execução

| Ação | Quando usar | Comportamento |
|---|---|---|
| **`atualizar`** | Etapa já concluída, quero o que é novo | Avança watermark / processa só o inédito. **É o botão principal.** |
| **`retomar`** | Etapa falhou ou foi cancelada | Recomeça do checkpoint, mesmo escopo do run anterior |
| **`refazer`** | Mudou modelo, prompt, threshold ou regra | Invalida o resultado da etapa e recomeça do zero |

`refazer` fica atrás de confirmação explícita mostrando o custo estimado. É a única ação que
queima investimento já pago.

### 4.2 Fingerprint e invalidação em cascata

Cada `run_etapa` concluída grava um `fingerprint`:

```
fingerprint = sha256(
    versao_codigo_da_etapa ||
    params_efetivos_json   ||
    fingerprint_de_cada_dependencia
)
```

Comparar o fingerprint gravado contra o recalculado responde sozinho *"esta etapa está
desatualizada?"* — sem manter grafo de invalidação na mão. (Ideia emprestada do Nix/Bazel, em
versão simples.)

**Invalidar nunca apaga.** Marca `desatualizada`. A interface mostra `⚠` e o usuário decide
quando reexecutar. Apagar automaticamente destruiria trabalho pago; entregar em silêncio um
export inconsistente seria pior ainda.

### 4.3 Modos de run

Modo é propriedade **do run**, gravada na criação, imutável depois.

| Modo | Comportamento |
|---|---|
| `assistido` | Padrão. Usuário dá play em cada etapa. |
| `sequencial` | Enfileira as etapas na ordem; ainda para em etapa marcada `precisa_gate`. |
| `amostra` | Pipeline inteira limitada a N documentos. **Ferramenta principal de teste.** |
| `simulacao` | Calcula escopo e custo estimado, não grava resultado nem chama provedor pago. |

O modo `amostra` merece destaque: vale mais que `simulacao` para validar uma mudança de config,
prompt ou provedor, porque produz resultado real por centavos em minutos.

### 4.4 Gates de aprovação

Um gate não é só "pausar". É um objeto com:

- **preview** — amostra do output (primeiros N termos, N pares candidatos)
- **estimativa** — nº de itens, chamadas de LLM, custo previsto, tempo previsto
- **edição** — o usuário altera o resultado antes de liberar (ex.: remover termos ruins)
- **ações** — aprovar / editar+aprovar / pular etapa / abortar run

Edições do usuário são gravadas como **override do run** (`run_etapa.params_override`), senão
retomar depois desfaz a edição silenciosamente.

Gates valem a pena em: **etapa 1** (termos — maior alavancagem: termo ruim contamina tudo e você
só descobre horas depois), **etapa 2** (volume descoberto), **etapa 3 e 6c** (as caras),
**etapa 4** (quanto sobrou antes de pagar OCR/LLM na 5).

## 5. Fluxo de dados repensado

A pipeline atual é estritamente sequencial em blocos. O novo desenho separa **descoberta** de
**processamento**, o que destrava duas coisas: saber o volume antes de gastar, e preservar o
dedup global da etapa 3.

```
FASE A — Descoberta (bloco, barata: só HTTP)
  0a  catálogo CATMAT/CATSER
  1   termos de busca por item          [GATE]
  2   busca no PNCP → documentos + itens da API (a "capa")   [GATE: volume]

FASE B — Classificação (dedup global sobre textos únicos)
  3   classifica (descricao, unidade) únicos → propaga p/ item_key
  4   corte: item com ≥1 categoria sobrevive               [GATE: escopo]

FASE C — Extração por documento (streaming, PDF descartável)
  5   baixa → extrai texto → enriquece item → DESCARTA o PDF
      estratégia por documento: janela | completa | visao

FASE D — Barreiras (precisam do corpus inteiro)
  6a  pares candidatos + rejeitor híbrido (BM25 + embedding)
  6b  reranker cross-encoder (GPU, custo zero de token)
  6c  LLM só na faixa ambígua                              [GATE: custo]
  7   agrupar por código, sanity de preço, ranking
  8   exportar XLSX PLASEG (+ delta incremental)
```

### 5.1 Consequência: o PDF vira efêmero

Hoje `data/arquivos/` acumula PDFs indefinidamente, e ~90% dos dados herdados apontam para
111 GB que ficaram no repositório antigo (`itens-contratos-atas-v2`). No novo desenho:

- baixa → extrai texto → grava texto + itens → **descarta o PDF**;
- guarda `url_pncp`, `hash_arquivo` e `n_paginas` para rebaixar sob demanda;
- o texto extraído (~1% do tamanho do PDF) cobre ~95% dos reprocessamentos.

Isso resolve de uma vez o problema de armazenamento **e** a dependência frágil dos caminhos
absolutos do v2.

### 5.2 Consequência: só se baixa o que sobreviveu

Hoje a etapa 2 baixa PDFs de todos os documentos encontrados, e só depois a 4 descarta itens.
No novo desenho a fase C roda **depois** do corte, então download e OCR de documento descartado
deixam de acontecer. É economia de banda, disco e CPU antes de qualquer economia de LLM.

## 6. Provedores plugáveis

Quatro capacidades **distintas** — não unificar numa interface só:

| Capacidade | Assinatura | Usada em | Provedores plausíveis |
|---|---|---|---|
| `chat` | `(prompt, schema) -> dict` | 1, 3, 5, 6c | GPU caseira (LM Studio), OpenRouter, qualquer OpenAI-compat |
| `embed` | `(list[str]) -> np.ndarray` | 6a | GPU caseira (bge-m3), Jina, Voyage, TEI |
| `rerank` | `(list[tuple[str,str]]) -> list[float]` | 6b | GPU caseira (bge-reranker), Cohere, Jina |
| `ocr` | `(png_bytes) -> str` | 5 | PaddleOCR caseiro, serviço pago |

Configuração fica **no banco**, não no `.env` — assim a URL do túnel ngrok da GPU (que muda de
tempos em tempos) é editável pela interface, sem redeploy.

### 6.1 Cuidados obrigatórios

1. **Embeddings não são intercambiáveis.** Trocar de provedor invalida o cache inteiro e mistura
   espaços vetoriais silenciosamente. A chave do cache **deve** ser
   `sha1(texto) + provedor + modelo + dimensao`.
2. **Fallback automático é proibido em `embed`.** Se a GPU cair no meio, cair para outro provedor
   corrompe o espaço vetorial. Em `embed`: **falhar e parar a etapa**. Em `chat`/`rerank`/`ocr`:
   fallback é aceitável.
3. **Trocar reranker exige recalibrar thresholds** (`RERANK_T_ACEITA` / `RERANK_T_REJEITA`).
   Já existe `ferramentas/calibrar_thresholds.py` como base.
4. **Cada adapter declara seu próprio `batch_size` e faz seu próprio retry/backoff.** GPU caseira
   aceita batch grande; APIs têm limite de RPM e de tokens por requisição.
5. **Teto de custo por capacidade e por run**, verificado *antes* de cada lote.

### 6.2 Health check

`GET /providers/status` testa cada provedor configurado. Roda automaticamente antes de qualquer
play. Evita descobrir na etapa 6a, 40 minutos depois, que o túnel da GPU caiu.

## 7. Estrutura de pastas alvo

```
pesquisa_precos/
  core/                 # regras puras — sem framework, sem I/O de rede, testável
    catalogo/  coleta/  classificacao/  extracao/
    pareamento/  agrupamento/  export/
  etapas/               # 1 módulo por etapa; cada um expõe executar()
    e0a_catalogo.py  e1_termos.py  e2_coletar.py  e3_classificar.py
    e4_cortar.py     e5_extrair.py  e6a_pares.py  e6b_rerank.py
    e6c_validar.py   e7_agrupar.py  e8_exportar.py
    registry.py         # metadados: nome, deps, custo, precisa_gate, params
  estrategias/          # implementações plugáveis da etapa 5
    janela.py  completa.py  visao.py
  providers/            # chat / embed / rerank / ocr + adapters
  db/                   # models SQLAlchemy, repositórios, migrations (alembic)
  runner/               # orquestração, lock, progresso, retomada, fingerprint
  api/                  # routers JSON, montados sob /api na app da web
  web/                  # app FastAPI (HTML + /api), templates Jinja2 + static
  config/               # settings, resolução de parâmetros; `paths.py` é só do importador
ferramentas/            # scripts de apoio pontuais (mantidos)
migracao/               # scripts one-shot CSV → Postgres
tests/
docs/
```

**Regras de dependência** (verificáveis por lint):

- `core/` não importa `db/`, `api/`, `web/`, `providers/`. Recebe dados, devolve dados.
- `etapas/` pode importar `core/`, `db/`, `providers/`.
- `api/` e `web/` importam apenas `services/` e `db/` — **nunca** `etapas/` diretamente.
- `web/` e `api/` consomem **os mesmos serviços**. Nenhuma página acessa o banco direto.
- `etapas/` **não importa `config/paths.py`** e não expõe nenhum `Path` (ADR-020). Um caminho
  de volta ali é o começo do caminho paralelo de novo: um arquivo que a web não serve, que o
  container não persiste e que ninguém lembra de migrar.

Essa última regra é o que preserva a saída: se um dia quiser um front separado, aponta para
`/api/*` e apaga os templates. Custo de migração ≈ zero.

## 8. Pontos de atenção conhecidos

| Risco | Mitigação |
|---|---|
| GPU remota cai no meio de 6a/6b | Retry + retomada por checkpoint (já existe). **Não** introduzir fila por causa disso. |
| Job morto deixa item travado em `processando` | Lease com expiração: `claimed_at + timeout` devolve o item à fila. |
| Resultado gravado mas estado não avançado → paga LLM duas vezes | Resultado + avanço de estado no **mesmo commit**. |
| Texto de PDF cresce sem limite (hoje 2,6 GB) | Política de retenção decidida na Fase 2, não depois. |
| Gate esquecido bloqueia o pipeline | Não bloqueia: o processo termina e libera o lock. Notificação avisa que há gate pendente. |
| `--novos` na primeira execução marca tudo como novo | Semear o snapshot a partir do último export oficial. Não é bug. |
| Perda do "usuário olhando o terminal" | Log estruturado por run acessível na UI + notificação. É o item que mais dói se faltar. |
