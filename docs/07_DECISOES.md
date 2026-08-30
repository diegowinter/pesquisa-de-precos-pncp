# 07 — Decisões de arquitetura (ADRs)

Cada decisão registra o contexto, as alternativas descartadas e as consequências. Um agente que
queira mudar uma destas precisa **argumentar contra o contexto registrado**, não apenas propor
algo diferente.

---

## ADR-001 — Monolito, não sistema distribuído

**Status:** aceita · 2026-08-16

**Contexto.** A execução da pesquisa de preços é **única**: não há múltiplos usuários rodando
pesquisas diferentes em paralelo. Vários usuários podem observar a mesma execução, mas o trabalho
é serial. O problema é *single-tenant, single-writer*.

**Decisão.** Um processo FastAPI + um subprocesso de execução por etapa + um PostgreSQL local.

**Alternativas descartadas**
- *Celery/RQ + Redis*: fila distribuída resolve concorrência entre trabalhos independentes —
  que não existe aqui. Overhead de operação sem retorno.
- *Prefect/Airflow*: DAG visual seria bom, mas traz um modelo de agendamento automático que
  contradiz o requisito de human-in-the-loop.
- *Microserviços*: nada a ganhar com um servidor e um operador.

**Consequências**
- Debug trivial: um log, um processo, um banco.
- Deploy = `systemd` + `git pull`.
- Cache quente em memória pode ser reaproveitado dentro de uma execução (catálogo, embeddings) —
  o que com workers separados se perderia.
- Se a GPU remota cair no meio de 6a/6b, o trabalho para. Aceito: resolve-se com retry +
  retomada por checkpoint, que já existe. **Não é motivo para introduzir fila.**

---

## ADR-002 — Subprocesso por etapa, não thread

**Status:** aceita · 2026-08-16

**Contexto.** As etapas pesadas (pandas sobre milhões de linhas, embeddings, OCR) seguram o GIL.
Se rodassem em thread dentro do processo web, travariam a API justamente quando o usuário mais
precisa dela (para ver progresso).

**Decisão.** O processo web dispara um **subprocesso** por etapa. Ele nasce, executa a etapa
inteira, grava o resultado, marca `concluida` e morre.

**Consequências**
- API sempre responsiva.
- "Cancelar" = matar o processo; a retomada por checkpoint garante que nada se perde.
- Deploy/reboot no meio de uma etapa deixa de ser problema (o estado está no banco).
- Custo: comunicação só pelo banco. Aceitável — o heartbeat em `run_etapa` já resolve progresso.

---

## ADR-003 — Interface servida pela própria aplicação

**Status:** aceita · 2026-08-16

**Contexto.** Ferramenta interna, um operador, sem equipe de front-end.

**Decisão.** FastAPI + Jinja2 + HTMX + Alpine.js, tudo no mesmo processo. Sem build step, sem npm.

**Alternativas descartadas**
- *SPA React/Next separada*: dois deploys, CORS, auth entre serviços. Custo sem benefício.
- *Streamlit/Gradio*: rápido de começar, mas o modelo de estado não comporta o hub de execução.

**Consequências**
- Editar e recarregar; iteração muito rápida.
- A API JSON existe de qualquer forma (para CLI e scripts), então a porta de saída fica aberta.
- **Regra que preserva a saída:** toda página consome os mesmos `services/` que a API. Migrar
  para um front separado depois custa ≈ zero.

**Quando reconsiderar:** múltiplos usuários com estado próprio, app móvel, ou alguém dedicado ao
front. Nenhum é o caso hoje.

---

## ADR-004 — Modelo barato é política, não flag

**Status:** aceita · 2026-08-16 · **Restrição nº 1 do projeto**

**Contexto.** Não há orçamento para o modelo caro (`OPENAI_MODEL_PASS2`). Hoje a etapa 6c usa o
modelo caro **por padrão** e só usa o barato com a flag `--fraco`. O comportamento seguro depende
de alguém lembrar de digitar uma flag — uma armadilha esperando acontecer.

**Decisão.** A flag `--fraco` deixa de existir. O modelo barato é o único permitido por padrão.
Usar modelo caro exige configuração explícita **acompanhada de teto de gasto**.

**Consequências**
- `capacidade_provedor` define o modelo; a etapa não escolhe.
- `run.teto_custo_usd` e tetos por capacidade são verificados **antes de cada lote**.
- `ctx.gastar()` levanta `TetoDeCustoExcedido` e interrompe a etapa de forma limpa.
- Toda chamada paga é registrada em `llm_chamada` — sem isso não há teto que funcione.

---

## ADR-005 — A etapa é a unidade de execução; nada avança sozinho

**Status:** aceita · 2026-08-16

**Contexto.** O valor do fluxo atual é o human-in-the-loop: o usuário vê o progresso e decide.
Ao ir para background, isso se perderia se a pipeline avançasse automaticamente.

**Decisão.** O usuário dá play em **uma etapa**. Ela roda até o fim, grava, marca-se concluída e
o processo morre. Ninguém avança para a próxima.

O estado por item continua existindo, mas apenas como **retomada dentro da etapa** — não é
mecanismo de fluxo.

**Consequências**
- Não é necessário `systemd timer` nem loop de reconciliação. Play é ação explícita.
- "Rodar tudo" é o mesmo mecanismo, apenas enfileirando as etapas em ordem.
- `run_etapa` fica trivial: `run_id, etapa, status, params, métricas, timestamps`.
- Aprovação de gate é um `UPDATE`, não um processo esperando. **Gate não segura o lock** — o
  processo já terminou.

---

## ADR-006 — Provedores abstraídos por capacidade, com fallback proibido em `embed`

**Status:** aceita · 2026-08-16

**Contexto.** Hoje a GPU caseira (túnel ngrok, URL que muda) atende embedder e reranker; o
OpenRouter atende chat; um servidor PaddleOCR atende OCR. Queremos poder apontar cada um para
outro provedor sem tocar em código de etapa.

**Decisão.** Quatro `Protocol` separados — `chat`, `embed`, `rerank`, `ocr` — configurados no
**banco**, não no `.env`.

**Consequências e cuidados obrigatórios**
1. **Cache de embedding chaveado por `(texto_hash, provedor, modelo, dimensao)`.** Sem isso,
   trocar de provedor mistura espaços vetoriais em silêncio — bug caro e difícil de achar.
2. **Fallback automático é proibido em `embed`.** Se a GPU cair no meio, cair para outro provedor
   corrompe o espaço vetorial. Em `embed`: falhar e parar a etapa. Permitido em `chat`, `rerank`
   e `ocr`.
3. **Trocar o reranker exige recalibrar** `rerank_t_aceita` / `rerank_t_rejeita`, usando a tabela
   `rotulo`.
4. Cada adapter declara seu `batch_size` e `rpm_limite`, e faz seu próprio retry com backoff.
5. Chave de API **nunca** vai para o banco — `provedor.api_key_ref` guarda o nome da env var.

**Benefício colateral:** health check por provedor antes do play, em vez de descobrir na etapa 6a,
40 minutos depois, que o túnel caiu.

---

## ADR-007 — Reprocessar é perda; caches são por conteúdo

**Status:** aceita · 2026-08-16

**Contexto.** O acervo atual representa investimento real em LLM e GPU: 302k itens enriquecidos,
320k textos classificados, 305k embeddings, 250k rótulos. O padrão de uso é "investe muito uma
vez, depois investe pouco na atualização".

**Decisão.** Tratar reprocessamento como **perda**, não como neutro.

**Consequências**
- Caches chaveados por conteúdo (`sha1(texto)` + modelo + versão do prompt) sobrevivem a um
  `refazer`. Refazer a etapa 3 com o mesmo modelo custa ~zero.
- **Nunca `DELETE` em resultado caro.** `refazer` invalida logicamente.
- O dedup da etapa 3 deixa de ser intra-execução e vira **permanente** (`texto_classificacao`),
  melhorando com o tempo.
- Custo acumulado por etapa aparece na interface — o usuário precisa *sentir* o que está prestes
  a descartar.

---

## ADR-008 — Três semânticas de reexecução

**Status:** aceita · 2026-08-16

**Contexto.** "Rodar de novo" não é uma coisa só. No pipeline atual, o comportamento natural já é
incremental (`--atualizar`) — não é exceção, é o padrão.

**Decisão.** Três ações explícitas: `atualizar` (o botão principal), `retomar` (após falha) e
`refazer` (do zero, com confirmação e custo estimado).

**Consequências**
- Nomes distintos evitam a confusão mais provável ("reexecutar apaga tudo?").
- `refazer` é o único que queima investimento — fica atrás de confirmação com custo na tela.
- `retomar` usa `params_efetivos` gravados, nunca recalcula: um run retomado não muda de
  comportamento porque alguém mexeu na config no meio.

---

## ADR-009 — Fingerprint em vez de grafo de invalidação

**Status:** aceita · 2026-08-16

**Contexto.** `refazer` a etapa 3 deveria invalidar 4→8. Manter esse grafo à mão é frágil.

**Decisão.** Cada etapa concluída grava
`fingerprint = sha256(versao_codigo || params_efetivos || fingerprints_das_dependencias)`.
Comparar o gravado com o recalculado responde sozinho "esta etapa está desatualizada?".

**Consequências**
- **Invalidar nunca apaga.** Marca `desatualizada`; a interface mostra `⚠`; o usuário decide.
  Apagar automaticamente destruiria trabalho pago; entregar export inconsistente em silêncio
  seria pior.
- `versao_codigo` é bumpado **manualmente** ao mudar a lógica de uma etapa. Esquecer de bumpar é
  o modo de falha conhecido — documentar em [08_CONVENCOES.md](08_CONVENCOES.md).
- Ideia emprestada do Nix/Bazel, em versão deliberadamente simples.

---

## ADR-010 — Etapa 5 com estratégias intercambiáveis e roteamento por documento

**Status:** ~~aceita · 2026-08-16~~ · **SUBSTITUÍDA pela [ADR-023](#adr-023) em 2026-08-29.**
As quatro estratégias e o roteamento saíram do repositório: sobre dado real, o desenho produziu
zero item confirmado em 4.159 documentos. O que ficou de pé é o contrato de saída
(`item_enriquecido`), que permitiu trocar a extração inteira sem tocar nas etapas 6 a 8.

**Contexto.** Duas abordagens de extração foram implementadas e medidas sobre o acervo real
(35.552 documentos, 291.044 itens, mediana de **2 itens/doc**, média 8,4, cauda até 1.305):

| Estratégia | Tokens de entrada | Observação |
|---|---:|---|
| `janela` (recorte por item) | 741,6 M | vence na mediana |
| `completa` (doc inteiro 1×) | 900,5 M | + 33 M de tokens de saída (mais caros) |
| **híbrida (roteada por doc)** | **458,3 M** | **−38%** |

Ponto de equilíbrio: `n_itens > tamanho_texto / 6500`. A distribuição é bimodal — documento magro
com 2 itens favorece a `janela`; documento gordo com muitos itens amortiza a `completa`.

**Decisão.** Etapa 5 com implementações plugáveis e roteamento **por documento** (`auto`),
não por run. Contrato de saída único (`item_enriquecido`), com uma coluna `estrategia`.

**Alternativas descartadas**
- *Só `janela`*: deixa 38% de economia na mesa.
- *Só `completa`*: mais cara no total e sofre "lost in the middle" em documentos de 15k–31k tokens.
- *Escolher a estratégia por run (config global)*: perde o ganho, que vem justamente de rotear
  caso a caso.
- *`visao` como caminho principal*: medição mostrou que é a mais cara — uma chamada por página,
  15 páginas para extrair 2 itens no documento típico. Fica como rota de exceção.

**Consequências**
- Custo de modelagem: **duas colunas** (`estrategia` em `documento_extracao` e em
  `item_enriquecido`). Sem tabela por estratégia, sem herança, sem polimorfismo.
- Etapas 6–8 leem só `item_enriquecido` e ignoram a estratégia.
- Fallback entre estratégias vira feature: documento `suspeito` pode ser reprocessado por outra
  rota, e é só reexecutar com a estratégia forçada.
- A `completa` **precisa herdar** da `janela`: confirmação por quantidade (fingerprint
  anti-PDF-trocado), banda de sanidade de preço, `doc_status`, e chunking com overlap para os
  5,6% de documentos acima de 40k tokens. Sem isso, os 38% vêm com regressão de qualidade.
- O divisor do roteamento fica em `config_valor`, recalibrável com custo real medido.

---

## ADR-011 — Separar descoberta de processamento

**Status:** aceita · 2026-08-16

**Contexto.** Processar documento a documento em streaming (baixar → OCR → classificar → extrair)
economizaria os 111 GB de PDFs. Mas destruiria o dedup por texto da etapa 3, que corta as
chamadas de LLM em ~5x — e essa é a restrição nº 1 do projeto.

**Decisão.** Duas naturezas de etapa:

- **Descoberta (bloco, barata):** obter a "capa" — metadados + itens da API — de todos os
  documentos de uma vez. Só HTTP.
- **Processamento (por documento):** download, extração e enriquecimento, **depois** do corte.

**Consequências**
- O dedup global da etapa 3 é preservado integralmente.
- Passa a ser possível **saber o volume antes de gastar** ("1.240 documentos, 38k itens, 7,9k
  descrições únicas → ~7,9k chamadas") — exatamente o número que um gate precisa mostrar.
- **Não se baixa mais PDF de documento que será descartado.** Hoje isso acontece: economia de
  banda, disco e CPU antes de qualquer economia de LLM.
- Dá para priorizar o processamento (documentos com mais itens inéditos primeiro).
- A etapa 2 deixa de baixar PDFs — mudança de responsabilidade a comunicar claramente.

---

## ADR-012 — PDF é efêmero; o texto extraído é o ativo

**Status:** aceita · 2026-08-16

**Contexto.** 111 GB de PDFs ficaram no repositório antigo (`itens-contratos-atas-v2`) e ~90%
das linhas herdadas apontam para caminhos **absolutos** lá. O texto já extraído está em
`5_pdf_texto.csv` (2,6 GB) e é o que se usa no dia a dia.

**Decisão.** Baixar → extrair texto → gravar → **descartar o PDF**. Guardar `url_pncp`,
`hash_arquivo` e `n_paginas` para rebaixar sob demanda.

**Consequências**
- Resolve armazenamento **e** a dependência frágil dos caminhos absolutos, de uma vez.
- `data/arquivos/` deixa de ser armazenamento e vira *scratch*: o PDF vive minutos.
- Reprocessar com um parser melhor continua possível a partir do texto (~95% dos casos). Para os
  outros, rebaixa-se do PNCP.
- **Requisito derivado:** implementar `url_documento(numero_controle_pncp, tipo_doc)`. Sem isso
  a decisão fica sem rede de segurança.
- Política de retenção do texto (180 dias após extração) definida em
  [02_SCHEMA.md §11](02_SCHEMA.md#11-retenção-e-limpeza).

---

## ADR-013 — Uma tabela `par`, não três

**Status:** aceita · 2026-08-16

**Contexto.** As etapas 6a, 6b e 6c produzem CSVs distintos, todos chaveados por `par_key`.

**Decisão.** Uma tabela `par` com colunas de todas as três fases, nulas até serem preenchidas.

**Justificativa.** Três tabelas com o mesmo PK exigiriam três joins em toda consulta e três
inserts em toda escrita, sem ganho de normalização real (a cardinalidade é 1:1:1).

**Consequência a observar na migração:** o CSV da 6b tem **mais** linhas que o da 6a (250.114 vs
220.781), porque acumula entre execuções resumíveis. Usar 6b como base do conjunto de `par_key` e
fazer `LEFT JOIN`, registrando quantos pares ficam sem score da 6a.

---

## ADR-014 — Config no banco, método no código

**Status:** aceita · 2026-08-16

**Contexto.** Queremos parametrizar sem deploy, mas sem que a configuração vire bagunça.

**Decisão.** **Vai para o banco o que muda a resposta; fica no código o que muda o método.**

| Banco (interface edita) | Código (PR + review) |
|---|---|
| thresholds, `min_itens`, `top_n`, faixas de preço | parsers da API do PNCP |
| termos de busca | lógica de agrupamento e menor preço |
| prompts e versões | schema das etapas, contrato de saída |
| modelo, provedor, URL da GPU, tetos | fórmula de score, algoritmo de corte |

**Duas salvaguardas obrigatórias**
1. **Config é versionada e imutável.** Editar cria versão nova; o run aponta para uma. Sem isso,
   "por que o resultado mudou?" fica sem resposta.
2. **Config gravada pela interface continua validada pelo schema Pydantic da etapa.** A interface
   não pode gravar valor que o código rejeitaria.

---

## ADR-015 — Resultados são append-only com `run_id`

**Status:** aceita · 2026-08-16

**Contexto.** O usuário quer rastrear de qual execução veio cada item, e comparar runs.

**Decisão.** Toda linha de resultado carrega o `run_id` de origem. Tabelas de resultado são
append-only com versão, não `UPDATE` in-place.

**Consequências**
- Ganha auditoria, diff entre runs e "refazer sem apagar" — os três de uma vez.
- Custo: mais linhas e a necessidade de saber "qual é a versão vigente" em cada consulta.
- **É a decisão de modelagem mais cara de mudar depois.** Se for para cortar escopo, corte no
  versionamento de config antes de cortar aqui.

---

## ADR-016 — A "regra dos 5" permanece desativada

**Status:** aceita · 2026-08 · reafirmada 2026-08-16

**Contexto.** `README.md` e `GUIA_IMPLEMENTACAO_PIPELINE.md` ainda descrevem uma "regra dos 5"
(mínimo/top-5 itens por código). Ela foi **desativada a pedido explícito do usuário** via
`MIN_ITENS=1` e `TOP_N=0`.

**Decisão.** Os defaults do sistema novo são `min_itens=1` e `top_n=0` (**sem teto** — traz todas
as referências confirmadas não sinalizadas por código).

**Consequência.** Mais de 5 itens por código é **comportamento esperado, não bug**. Isso já foi
investigado à toa em uma sessão anterior — o registro existe para não acontecer de novo.

---

## ADR-017 — Allow-list de PDMs sai do código e vira dado

**Status:** aceita · 2026-08-19

**Contexto.** A curadoria que define o escopo inteiro do projeto — quais PDMs de material e
quais códigos de serviço interessam à segurança pública — vive hardcoded em
`core/catalogo/local.py` (`PDMS_MATERIAIS`, `CODIGOS_SERVICOS`). Mudar o escopo da pesquisa
exige editar Python. Enquanto a pipeline rodava no laptop do usuário isso era apenas
incômodo; com a execução em servidor, passa a exigir **deploy para mudar curadoria**, o que
é inaceitável.

Isso sempre foi a exceção ao ADR-014 ("config no banco, método no código"): a allow-list é
config pura — uma lista de códigos, sem nenhuma lógica.

**Decisão.** Duas tabelas novas:

- `catalogo_raw` — CATMAT/CATSER **completos**, como vêm da API de Dados Abertos. Hoje esse
  dado só existe em parquet e nunca entra no banco.
- `pdm_permitido` — a allow-list curada, com `ativo`/`criado_por`/`criado_em`, no mesmo padrão
  de auditoria de `termo`.

`catalogo_item` deixa de ser carregado de um CSV já filtrado e passa a ser **derivado**:
`catalogo_raw ∩ pdm_permitido`, recomputável por SQL sempre que a curadoria mudar.

**Alternativas descartadas**
- *Manter no código e expor só leitura na web*: não resolve o problema real (mudar escopo sem
  deploy) e cria a ilusão de configurabilidade.
- *Arquivo de config versionado (YAML/JSON)*: continua sendo arquivo em disco, o que a Fase 12
  proíbe, e não tem auditoria de quem mudou o quê.

**Consequências**
- Escolher PDM pela interface exige o catálogo completo consultável — é o que justifica
  `catalogo_raw`, que de outro modo seria peso morto no banco.
- Mudar a curadoria passa a ter efeito rastreável: dá para saber quando um código entrou no
  escopo e quem o colocou.
- A allow-list vira parte do fingerprint da etapa 0a (ADR-009): mudar curadoria invalida o
  catálogo derivado, como deve ser.
- `core/catalogo/local.py` mantém o **método** (a função de filtro); perde os **dados**.
- **Complemento (2026-08-20, migration 0006):** `GRUPOS_MATERIAIS`/`GRUPOS_SERVICOS` seguem o
  mesmo caminho, em `grupo_permitido` — tabela SEPARADA, não uma coluna `especie` na mesma.
  As duas curadorias respondem a perguntas diferentes: `pdm_permitido` define o **escopo** (o
  que entra na pesquisa, aplicado sempre na derivação de `catalogo_item`); `grupo_permitido`
  define o **recorte do download** (quais `codigoGrupo` a 0a pagina com
  `--so-grupos-seguranca`, ignorado sem a flag). Fundi-las faria a tela misturar "o que eu
  pesquiso" com "o que eu baixo para poder escolher".

---

## ADR-018 — Nenhuma etapa escreve em disco

**Status:** aceita · 2026-08-19

**Contexto.** As etapas 0a–6c gravam toda a cadeia intermediária como arquivo em `data/`
(21 GB hoje). Isso é viável num laptop e inviável num servidor com filesystem efêmero, onde
um redeploy no meio de uma execução apaga o progresso. As etapas 7 e 8 já provaram o caminho
alternativo com `--fonte banco`.

**Decisão.** O banco é o único meio de persistência. Nenhuma etapa lê ou escreve arquivo —
nem de saída, nem de checkpoint, nem temporário.

Três consequências que não são óbvias e precisam ser tratadas nominalmente:

1. **Checkpoint deixa de ser arquivo e vira consulta sobre o próprio dado.** Não se cria
   tabela de checkpoint: "o que já processei" é derivável do resultado
   (`SELECT texto_hash FROM texto_classificacao`, `documento.estado`,
   `par.decisao_final IS NOT NULL`). Isso é mais correto que o CSV atual, que pode divergir do
   dado real quando o processo morre entre gravar o checkpoint e gravar o resultado.
2. **`export.arquivo` (caminho relativo) vira `export.conteudo` (`bytea`).** O XLSX é gerado
   em `BytesIO` e servido pela web a partir do banco.
3. **O PDF nunca chega ao container** — resolvido pelo ADR-019, não por buffer em memória.

**Alternativas descartadas**
- *Volume persistente no servidor*: resolveria a perda por redeploy, mas mantém duas fontes de
  verdade (banco + disco), impede escalar o processo horizontalmente e deixa o backup pela
  metade (`pg_dump` não cobre o volume).
- *Object storage (S3/R2)*: continua sendo um segundo sistema de persistência a operar,
  versionar e limpar, para dados que são naturalmente relacionais.

**Consequências**
- Uma execução sobrevive a restart do container: o progresso está no Postgres, não no disco.
- O backup do ADR/Fase 9 (`pg_dump`) passa a cobrir **tudo**, sem exceção.
- `PESQUISA_PRECOS_DATA` e boa parte de `config/paths.py` perdem função nas etapas migradas.
  Os caminhos permanecem enquanto `--fonte csv` existir (ver Fase 10).
- O dedup da etapa 3 deixa de ser intra-execução e vira permanente entre runs (ADR-007),
  porque `texto_classificacao` sobrevive onde o CSV era reescrito.

---

## ADR-019 — Todo processamento pesado é serviço externo; o servidor só orquestra

**Status:** aceita · 2026-08-19

**Contexto.** A Fase 7 já tratou LLM, embedding, rerank e OCR como capacidades resolvíveis por
provedor. Mas três cargas pesadas continuaram dentro do processo da etapa:

- **parse de PDF** (PyMuPDF): abre o documento e rasteriza páginas a 200 DPI;
- **BM25 + corte de pares** (etapa 6a): `rank-bm25` mais matrizes numpy sobre o produto
  catálogo × item — a carga que já causou um `MemoryError` real com ~33M linhas;
- **download do PDF**: banda e memória no container.

Com a execução em servidor, isso define o dimensionamento da máquina inteira: seria preciso
pagar por RAM que fica ociosa em 90% do tempo, para um pico que ocorre em duas etapas.

Um detalhe descoberto ao analisar a extração: **o caminho de OCR também depende do PyMuPDF**.
`ocr_pdf.rasterizar()` gera o PNG localmente e envia só a imagem — tirar o fitz do container
quebraria o OCR junto. Não é possível externalizar metade.

**Decisão.** Duas capacidades novas, no mesmo mecanismo de `providers/resolver.py`:

- **`pdf`** — recebe a referência do documento, **baixa o PDF ele mesmo**, extrai texto nativo
  por página, detecta páginas escaneadas, rasteriza e **chama o OCR internamente**. Devolve ao
  container apenas texto por página. O container nunca vê um byte de PDF.
- **`pareamento`** — recebe catálogo e itens, calcula BM25 e cosseno, aplica o corte top-K +
  piso em streaming e devolve **apenas os pares sobreviventes**.

O `pdf` absorver o OCR (em vez de devolver PNGs para o container repassar) é deliberado: a
regra crítica "nunca enviar o documento inteiro ao OCR, uma página por chamada" passa a ser
responsabilidade de quem tem o documento em mãos, e o servidor deixa de trafegar imagens.

**Alternativas descartadas**
- *`pdf` e `ocr` separados, com o container intermediando*: mantém a separação atual mas faz o
  servidor trafegar PNGs de 200 DPI sem nenhum uso próprio para eles.
- *Manter a 6a local por ser "só CPU"*: é justamente a etapa com histórico de estouro de
  memória; deixá-la dentro define o plano da máquina pelo seu pico.

**Consequências**
- O container perde `pymupdf`, `rank-bm25`, `sentence-transformers`/torch, `numpy` e `pandas`.
  Sobram FastAPI, SQLAlchemy/psycopg, Jinja2, requests e openpyxl.
- O corte em streaming da 6a continua existindo — muda de lado, não desaparece. O aviso de
  não reintroduzir um `aplicar_corte` pós-hoc passa a valer para o serviço externo.
- O health check pré-play do `runner.launcher` passa a cobrir `pdf` e `pareamento`: serviço
  fora do ar reprova a etapa **antes** de ela começar.
- **Custo:** o sistema deixa de funcionar sem os serviços externos no ar. É a troca consciente
  do ADR-001 (monolito) sendo parcialmente revista — o monolito continua valendo para
  *orquestração e estado*; só o processamento sai.
- Os serviços externos precisam de endereço estável. O túnel ngrok da máquina do usuário
  serve para desenvolvimento, **não** para o servidor em produção.

---

## ADR-020 — Uma superfície só: a web. Sem CLI, sem `--fonte`, sem `data/`

**Data:** 2026-08-22 · **Status:** aceito · **Fase:** 13

**Contexto.** O projeto acumulou **três** superfícies de operação para a mesma pipeline — a CLI
Typer (`pesquisa_precos/cli/`), o orquestrador de terminal (`rodar.py`) e a web em dois
processos (`web/` na 8001 + `api/` na 8000) — e **dois** meios de persistência, escolhidos por
`--fonte banco|csv` em cada etapa.

Isso não foi acidente: era a estratégia de migração. O ADR-018 pôs o banco como fonte da
verdade e a Fase 10 fez `banco` virar o default nas 12 etapas, mantendo `csv` como escape hatch
e rollback. Cumprido o papel, o custo de manter passou a superar o seguro:

- toda mudança de regra tinha que ser escrita duas vezes, nos dois ramos;
- a divergência entre os ramos **não levanta exceção** — produz um resultado diferente, tarde.
  A coluna `Unidade` divergindo em 473 de 8.154 linhas foi descoberta por comparação manual,
  não por erro;
- todo `Params` carregava um campo que só faz sentido em um dos ramos, e esse campo aparecia
  no formulário de configuração da web como se fosse uma escolha do operador;
- o ADR-002 já dizia que o processo web nunca executa etapa na própria thread. Com a CLI
  existindo em paralelo, havia dois jeitos de disparar a mesma etapa, e só um deles passava
  por lock, heartbeat, teto de custo e registro de custo.

**Decisão.** Uma superfície só.

1. **Um processo, uma porta.** `python -m pesquisa_precos` sobe a app da web, que monta os
   routers JSON sob `/api`. `api/app.py` deixa de existir; os routers ficam.
2. **A CLI sai inteira** — `cli/`, `rodar.py`, `limpar.py`, e o `main()` de cada etapa. O único
   ponto de entrada de execução é `runner/processo.py`, subido como subprocesso pela web.
3. **`--fonte csv` sai das 12 etapas.** Nenhuma etapa lê ou escreve arquivo. `VERSAO_CODIGO`
   vai a 2.0.0 em todas — o fingerprint tem que refletir que a origem dos dados mudou.
4. **`Params` fica.** Ele deixa de gerar flags e passa a gerar só o formulário da web
   (`services.config.schema_parametros`). Uma fonte, uma superfície.

**O que substitui o rollback que o `--fonte csv` era.** O repositório congelado
`../pipeline-csv-congelado/` — o pipeline CSV-only no commit anterior à refatoração, com os
20,87 GB de `data/` copiados e verificados. Voltar deixa de ser uma flag e passa a ser um
`git clone`; em compensação, o que se mantém no dia a dia é um caminho só.

**O que NÃO sai.** `config/paths.py` e `migracao/` (21 passos): 1,6 milhão de itens ainda vivem
só nos CSVs, e `migracao/` é o único código que sabe lê-los. Saem juntos, depois que o acervo
estiver no Postgres e validado. Enquanto isso, `paths.py` é do importador — nenhum módulo de
`etapas/` pode voltar a importá-lo, e `tests/test_estrutura.py` guarda essa regra (invertendo
a que existia antes, "todo caminho da etapa cai dentro de `data/`").

**Alternativas descartadas**
- *Manter a CLI para debug*: é o argumento que sustentou os dois caminhos por três fases. Na
  prática, "debug" vira o caminho de produção de quem tem pressa — e esse caminho não registra
  custo nem respeita lock.
- *Apagar a API JSON também*: os routers são cascas finas sobre `services/` e custam quase
  nada; sem eles não sobra nenhuma forma programática de comandar a pipeline.
- *Remover o CSV só depois de migrar o acervo*: seria a ordem ideal, e continua sendo a
  recomendação para EXECUTAR a migração. Mas o código do importador é independente do das
  etapas — segurar a limpeza pela migração só prolongaria a duplicação.

**Consequências**
- `core/io_seguro.py` (o `EscritorSeguro` append-only) perde todos os usuários e sai; o
  equivalente SQL é `db/repos/escrita.py`.
- `ContextoConsole` dá lugar a `ContextoNulo`: `estimar()` roda fora de um run e não precisa
  de `rich`. `ContextoBanco` continua sendo o contexto de execução real.
- `registry.caminho_erros` sai — erro por item já vive em `erro_item`.
- `typer` sai das dependências.
- **A Regra nº 1 do CLAUDE.md muda de forma, não de conteúdo:** quem opera a pipeline continua
  sendo o usuário, e o Claude continua sem disparar etapa. O que muda é que o usuário dá play
  no navegador em vez de digitar `uv run python -m ...` etapa por etapa.
- **Custo:** sem banco de pé, não há pipeline. Antes havia um caminho degradado; agora
  `DATABASE_URL` é pré-requisito duro. É o mesmo custo que o ADR-018 já havia aceitado, agora
  sem rede.

---

## ADR-021 — Trabalho pesado sai do repositório: só serviços, sem caminho em processo

> **Revisto em parte pela [ADR-023](#adr-023) (2026-08-29):** a capacidade `pdf` deixou
> de existir, e o serviço `pdf` do companion ficou órfão. O princípio segue valendo para
> `matching`; o que mudou é que a etapa 5 não processa mais byte de PDF em lugar nenhum —
> ela baixa o arquivo (I/O) e o entrega ao modelo.

**Data:** 2026-08-22 · **Status:** aceito · **Substitui parcialmente:** [ADR-019](#adr-019)

**Contexto**

A ADR-019 tirou o processamento pesado do processo da etapa, mas manteve as duas formas: sem
`PDF_BASE_URL`/`PAREAMENTO_BASE_URL`, os adapters `…EmProcessoAdapter` rodavam PyMuPDF, BM25 e
sentence-transformers na própria máquina. O mesmo valia para `embed` e `rerank`, que escolhiam
entre GPU remota e modelo local pelo parâmetro `remoto`.

Era a mesma estrutura que o `--fonte csv` da [ADR-020](#adr-020): dois caminhos para o mesmo
resultado, escolhidos por configuração, e a divergência entre eles não levanta exceção. Pior
aqui, porque o caminho em processo é o *default* de quem não configurou nada — o modo em que
um erro de configuração vira, silenciosamente, torch carregado no servidor que orquestra.

O destino da aplicação é um **servidor econômico**, dimensionado para scraping do PNCP e
escrita no banco. GPU e CPU intensiva nunca vão rodar lá. Um caminho em processo que nunca
será usado em produção é código que só existe para divergir.

**Decisão**

O trabalho pesado sai do repositório e vira um repositório companion,
**`pncp-servicos-locais`**, com quatro serviços HTTP (`gpu`, `ocr`, `pdf`, `pareamento`).
Aqui ficam apenas clientes.

Saem daqui, movidos para `servicos/core/` de lá: `providers/pdf_pipeline.py`,
`providers/ocr_pdf.py`, `providers/embedder_local.py`, `providers/reranker_local.py` e
`core/pareamento/` (motor + índice BM25). Junto foram os testes do motor.

Saem os quatro adapters em processo. `resolver._exigir_servico` transforma `base_url` vazio em
`SystemExit` com o nome da variável a preencher — a etapa para antes de começar, e o health
check pré-play reprova em vez de aprovar.

**A linha do corte é "precisa de GPU ou é CPU intensiva", não "toca em bytes".**

- Fica **aqui**: baixar os PDFs do PNCP (I/O barato, e o cliente da API já existe para a
  etapa 2), orquestrar, gravar no banco.
- Vai para **lá**: parse com PyMuPDF, rasterização a 200 DPI, OCR, embedding, rerank, BM25 e o
  corte top-K sobre o produto catálogo × itens.

Consequência direta no contrato da capacidade `pdf`: este processo baixa os arquivos e os
manda por upload multipart; o serviço devolve texto por página. O companion não sabe o que é
um contrato, uma ata ou um `tipoDocumentoNome` — recebe arquivos, devolve texto.

**Alternativas descartadas**

- *Deixar o companion importar `pesquisa_precos` por dependência de caminho*: foi a primeira
  versão. Funciona, mas amarra os dois repositórios ao mesmo disco e inverte a autonomia de
  quem hospeda o serviço.
- *Copiar o código para os dois lados*: é a duplicação que a ADR-019 recusou, e com razão — o
  limiar de página escaneada, o DPI e o piso do corte divergindo dariam texto e pares
  diferentes conforme o serviço estivesse no ar, sem erro nenhum.
- *Levar o download para o serviço, junto com o cliente do PNCP*: daria dois clientes da mesma
  API pública para manter em sincronia, sem tirar carga nenhuma do servidor — baixar é I/O.
- *Manter o caminho em processo só para desenvolvimento local*: rodar local também é rodar os
  serviços. Um `python -m servicos pdf` a mais é mais barato que uma segunda implementação.

**Consequências**

- `ocr` deixa de ser capacidade deste processo: quem chama o OCR é o serviço de `pdf`, na
  máquina dele. `OCR_BASE_URL`/`OCR_MODEL`/`OCR_API_KEY` saem do `.env` daqui e passam para o
  do companion. A etapa 5 declara `("pdf", "chat")`; a 6a declara `("pareamento",)`.
- O extra `localmente` sai do `pyproject.toml`. Não há mais como instalar o pesado aqui.
- `remoto`, nas etapas 6a/6b, deixa de escolher: só existe o serviço de GPU. O campo sobrevive
  nas assinaturas sem efeito, como `fraco` na 6c depois da ADR-004.
- **Custo:** rodar o pipeline na própria máquina passa a exigir subir os serviços. É o preço
  de ter uma forma canônica, e é o mesmo que a ADR-020 cobrou ao tirar a CLI.
- **Rollback:** os módulos movidos estão no histórico deste repositório (o commit anterior a
  esta ADR) e vivos em `pncp-servicos-locais`. Nada foi perdido.

---

## ADR-022 — Configuração inteira no banco; segredo cifrado, com uma chave-mestra só

**Data:** 2026-08-22 · **Status:** aceito · **Estende:** [ADR-014](#adr-014) ·
**Revoga:** a nota "chave de API nunca vai para o banco" de docs/02_SCHEMA.md §10

**Contexto**

A ADR-014 mandou "modelo, provedor, URL da GPU, thresholds" para o banco, e a ADR-006 montou a
resolução por capacidade lendo banco → `.env`. Na prática, nada disso chegou ao operador: o
banco de provedores está vazio, `capacidade_provedor` nunca foi populada, e **não existe rota
que escreva nessas tabelas** — `upsert_provedor`/`apontar_capacidade` só podem ser chamados por
SQL na mão. `/provedores` é uma tela de leitura. O resultado é que a configuração real da
aplicação — modelo do LLM, endereço do túnel da GPU, thresholds do reranker — mora num arquivo
`.env` editado à mão, e trocar qualquer coisa exige acesso ao disco do servidor e reinício.

Havia ainda uma segunda fronteira, definida no §10 do schema: `provedor.api_key_ref` guardava o
*nome* de uma env var, nunca a chave. A intenção era boa (não vazar segredo em `pg_dump`), mas
o efeito era que cadastrar um provedor novo pela tela continuava impossível sem editar o `.env`
e reiniciar — a tela ficava a meio caminho, e o "plugável" nunca acontecia.

**Decisão**

**Toda a configuração vai para o banco, segredo incluído.** Fora do banco sobra exatamente uma
coisa: a chave-mestra que decifra os segredos.

1. `provedor.api_key_ref` é substituída por `api_key_cifrada bytea` + `api_key_last4 text` +
   `api_key_key_id text`. A cifra é AES-GCM (`cryptography`), envelope simples: a chave de
   dados é cifrada por uma **chave-mestra** (`APP_SECRET_KEY`), que vem do **ambiente do
   processo** — não de arquivo no repositório. Um `pg_dump` sozinho não decifra nada.
2. A chave **nunca** volta pela API nem pelo HTML. Campo write-only; a tela mostra
   `sk-or-…7b9d` (`api_key_last4`) e a data da última troca. Decifrar só acontece em processo,
   em `providers/resolver`, no momento de montar o adapter.
3. `api_key_key_id` existe desde o primeiro dia para permitir **rotação** da chave-mestra sem
   downtime: re-cifra linha a linha, com as duas chaves aceitas durante a janela.
4. Os thresholds (`rejeitor_threshold`, `rerank_t_aceita`, `rerank_t_rejeita`, `min_itens`,
   `top_n`) deixam de ser lidos de `ctx.config` e viram campos de `Params` da etapa que os usa.
   Isso os coloca automaticamente no formulário gerado pelo Pydantic
   (`services.config.schema_parametros`) e, o que importa mais, sob `config_versao` — versionado
   e imutável, como a ADR-014 exige.
5. `_resolver_via_env` **sai**. Provedor não configurado é erro na tela de provedores, não
   silêncio com o modelo errado. Uma migração Alembic semeia `provedor`/`capacidade_provedor` a
   partir do `.env` de hoje, uma vez, para que a virada não exija recadastro manual.

**A linha de corte é "configurável vs. bootstrap"**, não "esvaziar o `.env`". O `.env`
continua existindo e continua sendo o lugar certo para o que a aplicação precisa saber *antes*
de conseguir ler o banco: `DATABASE_URL` e `APP_SECRET_KEY`. Nenhum dos dois é ajuste de
operação — não se troca o banco da aplicação pela tela, e a chave-mestra não pode morar no que
ela protege. Tudo o que resta no `.env` hoje (modelo, `base_url`, thresholds, batch, custos,
chaves de provedor) é configuração de operação e vai para o banco. No destino (F12) os dois
sobreviventes são variáveis de ambiente do serviço; o arquivo é conveniência de
desenvolvimento.

**Alternativas descartadas**

- *Manter `api_key_ref` (env var por nome)*: é o estado atual, e é justamente o que impede
  cadastrar provedor pela tela. Meia-medida: a tela existe mas não basta.
- *Cofre de segredos (Vault/KMS/Secrets Manager)*: segurança melhor, infra incompatível com o
  "servidor econômico" da ADR-021. Para um operador só, o ganho não paga o custo.
- *Keyring do sistema operacional*: funciona na máquina do operador, dá dor de cabeça em
  servidor headless — e o runner roda como subprocesso, que herda mal essas sessões.
- *Digitar a chave-mestra ao subir o servidor*: máxima segurança, incompatível com um serviço
  que precisa reiniciar sozinho.
- *Guardar a chave em texto puro na coluna*: o `pg_dump` entre agregados (procedimento normal
  aqui) passaria a carregar a chave viva em todo backup.

**Consequências**

- Dependência nova: `cryptography`. É a única adição de peso, e roda em qualquer servidor.
- **Sem `APP_SECRET_KEY` não há provedor.** É a mesma dureza que a ADR-020 impôs com
  `DATABASE_URL`: um caminho só, que falha alto. A app recusa subir sem ela.
- `config/settings.py` encolhe para segredos e `DATABASE_URL`; `carregar_config()` deixa de ser
  a fonte de thresholds e URLs. Bumpar `VERSAO_CODIGO` de toda etapa cujo threshold migrou —
  o valor efetivo passa a vir de outro lugar, e o fingerprint tem de enxergar isso.
- **Rollback:** as migrações são reversíveis (`downgrade` de `0009`/`0010`), e o `.env` atual
  continua no disco do operador enquanto a virada não é confirmada.

**Ajustes feitos na implementação** (2026-08-22)

- O **seed ficou em `tools/seed_providers.py`**, não numa migração Alembic como esta
  ADR previa. Semear depende do `.env` de origem e da chave-mestra no ambiente; migração que
  exige segredo para rodar quebra em qualquer máquina que não seja a do operador, e o
  `downgrade` dela não teria como desfazer a cifra.
- **`provedor.custo_usd_chamada` (migração 0010)** não estava prevista. `CUSTO_USD_CHAMADA_*`
  não cabia em `custo_in_por_mtok`/`custo_out_por_mtok`: converter preço por chamada em preço
  por Mtok exigiria inventar um tamanho médio de prompt, e o `estimar()` prefere responder
  "não estimado" a inventar. `NULL` = não informado; `0.0` = grátis (o provedor local).
- **`REJEITOR_THRESHOLD` não migrou porque já estava morto** — a 6a usa o `Param` `piso` desde
  a Fase 11. Só saiu de `carregar_config()`.
- **`estimar()` usa `resolucao_opcional`**, que devolve `None` em vez de levantar. Estimativa
  roda quando o operador ABRE a tela da etapa, antes de qualquer play, e nesse momento é normal
  a configuração ainda estar incompleta — derrubar a tela esconderia os números que ele foi ali
  ver. O gate de verdade continua no play (`checar_saude_previa`).
- **`Curador.from_provedor` foi removido.** Era um segundo caminho, usado só pela 6c, que
  montava o cliente lendo o `.env` e contornava `capacidade_provedor` inteiro — exatamente o
  tipo de duplicação que esta ADR existe para eliminar.

---

## ADR-023 — Etapa 5: uma extração só, com o documento inteiro como anexo

> **Diagnóstico corrigido pela [ADR-024](#adr-024) (2026-08-29):** os "zero itens confirmados
> em 4.159 documentos" citados abaixo NÃO vinham só do desenho das estratégias plugáveis.
> Metade da causa era a duplicação de itens entre atas da mesma compra — a etapa perguntava,
> para cada ata, por 82 itens dos quais só 3 podiam estar ali. A troca da extração continua
> justificada (e produziu o primeiro `pdf_ok` do projeto), mas sozinha não teria resolvido.

**Contexto.** A [ADR-010](#adr-010) desenhou a etapa 5 com quatro caminhos — `window`, `full`,
`vision` e o roteamento `auto` entre eles — apostando que documentos diferentes pedem
estratégias diferentes. O primeiro teste assistido sobre dado real (2026-08-28) mediu a aposta:

- **zero itens confirmados em 4.159 documentos.** `fonte_descricao` ficou 100% `api` — ou seja,
  nenhuma descrição do PDF chegou ao produto, que é a única coisa que a etapa existe para fazer;
- `nao_encontrado` 196, `qtd_nao_confere` 52, `sem_texto` 62, `doc_status` 248 suspeito e 62
  ilegível, **0 extraído**;
- o `auto` escalava para `vision` em ~57% dos documentos, contra um modelo de chat cujo
  `input_modalities` é `['text']`. Falha garantida, no ponto mais caro do fluxo;
- ~7 documentos/minuto — 10 horas para a fila, para não produzir nada.

Ao lado disso, três bugs sérios corrigidos na mesma semana (código morto depois de um `return`
que devolvia `None` para 1.146 documentos; `NOT NULL` violado em `tokens_in`; `ok` vs
`extraido` no enum `estado_documento`) tinham a mesma origem: **um fluxo com chaveamento demais
para o que entrega**. Cada caminho tinha o seu jeito de gravar, e o caminho bom era o menos
exercitado.

O que resolveu o problema não foi um quinto caminho. Foi anexar o PDF inteiro num chat e pedir
"retorne a tabela de itens" — que devolveu a tabela correta, com marca e modelo, em 2,9 s e
US$ 0,0025.

**Decisão.** Um caminho só, sem estratégia, sem roteamento, sem escalonamento. Duas chamadas de
LLM por documento:

1. **Extração** — o PDF vai como anexo (`type: file`, base64) para a capacidade nova
   **`extract`**, e volta a **tabela de itens em texto, "as it is"**.
2. **Casamento** — para cada item da API, uma chamada (`chat`) que recebe **só essa tabela** e
   devolve descrição completa, preço, quantidade e fornecedor.

**Por que texto livre, e não colunas.** Cada documento traz as colunas que tem: um traz
fornecedor e modelo, outro só descrição/quantidade/preço. Um esquema fixo obrigaria o modelo a
preencher campo inexistente — que é convite para inventar. `documento_extracao.tabela_texto`
guarda a resposta como veio; quem estrutura é a segunda chamada, item a item, contra a âncora
que a API já fornece.

**Por que uma capacidade nova, e não a `chat`.** São modelos diferentes com preços diferentes:
`chat` é o barato que classifica texto ([ADR-004](#adr-004)) e a etapa 3 faz dezenas de milhares
de chamadas com ele. `extract` precisa aceitar documento. Uma capacidade só obrigaria a escolher
entre pagar caro na etapa 3 ou não conseguir ler PDF na 5.

**Consequências.**

- **`documento_pagina` foi dropada.** Era o gigante do banco (888 mil linhas, 2,6 GB) e ninguém
  a jusante a lia: as etapas 6 a 8 leem de `item_enriquecido` apenas `descricao_final` e
  `destino`. Com ela saíram a política de retenção de página e a migração `m10`.
- **O contrato de saída não mudou.** Foi possível trocar a extração inteira sem tocar em
  nenhuma etapa a jusante — a prova de que o corte da [ADR-010](#adr-010) em `item_enriquecido`
  estava certo, mesmo com o resto do desenho errado.
- **A capacidade `pdf` deixou de existir**, e com ela o cliente do serviço `pdf` do companion.
  O serviço continua no repositório `pncp-servicos-locais`, mas **órfão**: nada mais o chama, e
  não precisa ser subido para a etapa 5 rodar. Idem para o `ocr`, que já não tinha cliente desde
  a [ADR-021](#adr-021).
- **O download voltou para este processo.** Não contradiz a ADR-021: baixar do PNCP é I/O
  barato, e o cliente da API já vive aqui desde a etapa 2. O que a ADR-021 tirou daqui foi o
  trabalho de GPU e de CPU intensiva — e agora nem isso acontece, porque nenhum byte de PDF é
  processado deste lado: ele é lido e enviado.
- **Circuit breaker na etapa 5.** Vinte extrações seguidas falhando sem nenhuma dar certo abortam
  a etapa. É o mecanismo que faltou na etapa 3 em 2026-08-25, quando um modelo aposentado no
  OpenRouter produziu falha silenciosa em série.

**O que se perde.** Não há mais como reprocessar um documento "com a outra estratégia" e
comparar. Era o argumento central da ADR-010 — e em nenhuma execução real chegou a ser usado
para decidir coisa alguma, porque nenhuma das estratégias confirmou item. Se um dia houver duas
formas de ler documento que valham a pena comparar, a comparação volta como decisão nova, não
como código adormecido.

---

## ADR-024 — O item pertence à COMPRA, e o casamento é uma chamada por documento

**Status:** aceita · 2026-08-29

**Contexto.** A API do PNCP entrega itens **por compra**. Não existe rota de itens por ata —
confirmado na especificação OpenAPI oficial (109 rotas; as de ata são `atas`, `atas/{seq}`,
`arquivos`, `contratos`, `partesenvolvidas`, `historico`, nenhuma de item). O endpoint
`/itens/{n}/resultados` traz fornecedor e preço homologado, mas aponta para
`numeroControlePNCPCompra`, não para a ata.

Como a etapa 2 varre atas, ela pendurava a lista inteira da compra em **cada ata**. Medido no
acervo:

| | linhas | itens reais | fator |
|---|---:|---:|---:|
| ata | 267.205 | 31.822 | **8,40×** |
| contrato | 43.889 | 43.889 | 1,00× |
| **total** | 311.094 | 75.711 | **4,11×** |

O caso que revelou o problema: o pregão 507 da Embrapa tem 88 itens e **25 atas**, uma por
fornecedor. A ata 00062 registra 3 itens (DARLU: mouse pad, apoio de punho, descanso de pés);
a 00061 registra 55; a 00065, um. As 25 receberam os mesmos 82 itens homologados.

Isso destruía a etapa 5: no teste de 2026-08-29 ela confirmou **1 item em 89**, com 12 de 13
documentos marcados `suspeito`. A extração estava certa — a ata 00062 devolveu exatamente as 3
linhas da DARLU. O `nao_encontrado` também estava certo: o Coturno não está numa ata de mouse
pad. **O que estava errado era a pergunta.** Dos 34.256 pares (item, documento) que a etapa
tinha para casar, ~71% eram impossíveis de responder com "sim" por construção.

> Isso corrige em parte o diagnóstico da [ADR-023](#adr-023), que atribuiu os "zero itens
> confirmados em 4.159 documentos" apenas ao desenho das estratégias plugáveis. A duplicação
> pesava tanto quanto, e a abordagem antiga sofria dela igualmente.

**Decisão.**

1. **A identidade do item é a compra.** `item_key` passa de `<documento>::<item>` para
   `<chave_compra>::<item>`, e `item.numero_controle_pncp` (com o FK para `documento`) sai.
   Era essa amarra que criava a duplicação: cada linha nascia presa a uma ata.
2. **O vínculo ata↔item nasce na etapa 5**, em `item_enriquecido.numero_controle_pncp`, que
   entra na chave primária. É onde ele é de fato descoberto — lendo a tabela do PDF.
3. **O casamento vira uma chamada por documento**, levando a tabela extraída e os candidatos
   da compra juntos. De 34.256 chamadas para 3.347.

**A chave de compra é o prefixo até o ano**, e existe numa função só
(`core.collection.urls.chave_compra`):

```
ata:      00348003000110-1-000507/2025-000004  ->  00348003000110-1-000507/2025
contrato: 01664910000131-2-000068/2026         ->  01664910000131-2-000068/2026
```

Para contrato coincide com o próprio documento — mesma regra, mesmo resultado, sem caso
especial (1.661 de 1.661 verificados). Um `regexp_replace` equivalente escrito à mão em SQL
durante esta investigação perdeu o ano por um backslash comido pelo shell e produziu contagens
erradas sem levantar erro; `tests/test_estrutura.py` guarda a exclusividade da função.

**Por que não deduplicar na etapa 4.** Marcar `sobrevivente` numa ata só por item real exigiria
escolher a ata **antes** de saber qual delas contém o item — que é justamente o que a etapa 5
descobre. Foi descartado por isso.

**Por que não mandar todas as tabelas da compra numa chamada por item.** Foi a primeira ideia,
e não economiza nada: 82 itens × 25 tabelas é o mesmo produto cartesiano de 25 atas × 82
itens. Cai o número de chamadas, não o de tokens — e é token que se paga.

**Por que não vincular ata→fornecedor.** Cada ata é de um fornecedor, e o item já tem
`fornecedor` no banco; bastaria saber o fornecedor de cada ata para filtrar. Mas a API não o
expõe (`/atas/{seq}` não traz, `partesenvolvidas` só lista órgãos), então viria do PDF — e
duas alternativas determinísticas caíram por fragilidade: casar pelo número do item (muitos
documentos não numeram) e casar por preço (não se sabe se a tabela traz o homologado inicial
ou o do vencedor). O casamento em lote dispensa o vínculo: ele o **produz**.

**Consequências.**

- **`doc_status` ganha `fora_de_escopo`.** Uma ata cujos candidatos não incluem nenhum item
  dela é um documento perfeito fora do escopo, não um PDF ilegível. A regra de `suspeito`
  **não** muda: é absoluta (zero confirmados), não proporcional — 3 de 82 é `ok`.
- **Falha de casamento vira `status = 'erro'`**, não `nao_encontrado`. Eram indistinguíveis, e
  foi essa confusão que escondeu a falha em massa da etapa 3 em 2026-08-25.
- **A etapa 2 também economiza.** Ela refazia `fetch_itens()` e um `fetch_resultado_vencedor()`
  por item em cada ata — mais de 2.200 chamadas para o pregão 507. Um cache por compra derruba
  para 89.
- **As contagens mudam de escala.** A etapa 4 vai de 34.256 para 9.886 sobreviventes. Não se
  perdeu acervo: sumiu a duplicação.
- **A migração é destrutiva por natureza.** O `downgrade` recria as colunas e devolve cada item
  a UMA ata da compra, mas a associação original não volta — era justamente a associação errada
  que esta ADR remove.

**Pendência conhecida.** Um item pode ter mais de um fornecedor homologado?
`fetch_resultado_vencedor` escolhe **um** vencedor e guarda só ele, então o banco não pode
responder — a ausência de divergência de preço entre atas é consequência disso, não evidência.
Não é regressão (é o comportamento que já existia), mas é o único ponto que poderia afetar a
correção do preço, e merece medição antes de o produto ser publicado.
