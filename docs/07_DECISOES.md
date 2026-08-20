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

**Status:** aceita · 2026-08-16

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

## ADR-019 — Todo cômputo pesado é serviço externo; o servidor só orquestra

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
- O health check pré-play do `runner.executor` passa a cobrir `pdf` e `pareamento`: serviço
  fora do ar reprova a etapa **antes** de ela começar.
- **Custo:** o sistema deixa de funcionar sem os serviços externos no ar. É a troca consciente
  do ADR-001 (monolito) sendo parcialmente revista — o monolito continua valendo para
  *orquestração e estado*; só o cômputo sai.
- Os serviços externos precisam de endereço estável. O túnel ngrok da máquina do usuário
  serve para desenvolvimento, **não** para o servidor em produção.
