# Documentação de projeto — Pesquisa de Preços PLASEG

Esta pasta contém o **projeto de engenharia** da transformação da pipeline atual (scripts CLI +
CSV) em uma aplicação de verdade (monolito Python com banco, API e interface web).

> **Estado:** projeto aprovado, desenvolvimento **não iniciado**. Estes documentos são o
> contrato para os agentes/desenvolvedores que vão implementar. Nada aqui foi codificado ainda.

## Ordem de leitura

| # | Documento | Para quê |
|---|---|---|
| 01 | [ARQUITETURA.md](01_ARQUITETURA.md) | Visão geral, decisões estruturais, o que é e o que **não** é o sistema |
| 02 | [SCHEMA.md](02_SCHEMA.md) | DDL completo do PostgreSQL, com justificativa e dimensionamento |
| 03 | [ETAPAS.md](03_ETAPAS.md) | Contrato de etapa, registry, especificação etapa por etapa |
| 04 | [FASES.md](04_FASES.md) | Roteiro evolutivo: o que cada fase entrega e como validar |
| 05 | [MIGRACAO.md](05_MIGRACAO.md) | Migração dos CSVs atuais para o banco, sem perder o acervo |
| 06 | [API_E_WEB.md](06_API_E_WEB.md) | Endpoints, telas, o "hub" de execução estilo GitLab |
| 07 | [DECISOES.md](07_DECISOES.md) | ADRs — decisões com contexto, alternativas e consequências |
| 08 | [CONVENCOES.md](08_CONVENCOES.md) | Padrões de código, nomenclatura, testes — leitura obrigatória p/ agentes |

## Documentos legados (do pipeline atual)

- [../README.md](../README.md) e [../GUIA_IMPLEMENTACAO_PIPELINE.md](../GUIA_IMPLEMENTACAO_PIPELINE.md)
  — descrevem a pipeline **como ela é hoje**. Continuam válidos como referência de regra de
  negócio, mas **contêm a "regra dos 5" que foi desativada** (ver [../CLAUDE.md](../CLAUDE.md)).
- [../CLAUDE.md](../CLAUDE.md) — contexto de trabalho, restrições de custo, estado da validação.

## Regras invioláveis do projeto

Estas atravessam todos os documentos. Um agente que as violar produziu código errado, mesmo que
o código funcione.

1. **Custo de LLM é a restrição nº 1.** Não existe orçamento para modelo caro. O modelo barato é
   o *default estrutural*, não uma flag. Ver [ADR-004](07_DECISOES.md#adr-004).
2. **Reprocessar é perda.** Todo resultado caro (classificação, extração, embedding, veredito) é
   ativo permanente e cacheado por conteúdo. Ver [ADR-007](07_DECISOES.md#adr-007).
3. **A pipeline nunca avança sozinha.** Quem dá play em cada etapa é o usuário.
   Ver [ADR-005](07_DECISOES.md#adr-005).
4. **Rastreabilidade ponta a ponta.** De qualquer linha do export deve ser possível navegar até
   o item, o documento, a URL no PNCP, a classificação (com prompt e modelo), o par e o grupo.
5. **A "regra dos 5" está desativada** (`MIN_ITENS=1`, `TOP_N=0`). Mais de 5 itens por código é
   comportamento esperado, não bug.
