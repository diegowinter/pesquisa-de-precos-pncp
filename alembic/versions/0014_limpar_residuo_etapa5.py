"""dropa n_paginas e limpa o residuo das rodadas quebradas da etapa 5

Duas faxinas que sobraram das ADR-023 e ADR-024:

1. `n_paginas` MORREU na ADR-023, nas DUAS tabelas onde existia (`documento_extracao` e
   `documento`). Desde que a etapa 5 deixou de parsear PDF — ela baixa o arquivo e entrega ao
   modelo —, este processo nunca sabe quantas paginas o documento tem. Nenhuma das duas jamais
   foi preenchida: 0 linhas em 5.698 documentos.

   A ADR-012 prometia guardar `url_pncp`, `hash_arquivo` e `n_paginas` para "rebaixar sob
   demanda", e so a primeira foi implementada. `n_paginas` sai por ser inalcancavel;
   `hash_arquivo` FICA e passa a ser preenchida pela etapa 5, que ja tem os bytes do PDF em
   maos para manda-los ao modelo (`repos.documento.gravar_hash_arquivo`). E o hash que permite
   conferir, ao rebaixar do PNCP, que o arquivo e o mesmo que gerou a tabela extraida.

2. O RESIDUO das duas rodadas da etapa 5 que rodaram com o desenho errado (2026-08-28 e
   2026-08-29). Sao 3.466 vereditos em `item_enriquecido` produzidos perguntando, para cada
   ata, por itens que pertenciam as outras 24 atas do mesmo pregao — 3.187 `nao_encontrado`,
   216 `qtd_nao_confere`, 62 `sem_texto` e 1 `pdf_ok`. Nao sao dados ruins: sao respostas
   certas para perguntas erradas, e as etapas 6-8 leem `destino` e `descricao_final` desta
   tabela sem saber disso.

   Junto vai `documento.estado`. A chave de resumo da etapa 5 e `estado = 'extraido'`, e havia
   1 documento nesse estado — ele seria PULADO para sempre, inclusive pelo botao "Refazer",
   que mexe em `run_step`/`item_error` mas nao no estado do documento.

O downgrade recria a coluna VAZIA e nao ressuscita os vereditos: eles vieram de um caminho de
codigo que nao existe mais, e reproduzi-los exigiria rodar a etapa antiga de novo.

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE documento_extracao DROP COLUMN IF EXISTS n_paginas;
ALTER TABLE documento           DROP COLUMN IF EXISTS n_paginas;

DELETE FROM item_enriquecido;
DELETE FROM documento_extracao;

-- De volta para a fila da etapa 5. `fora_de_escopo` NAO entra: quem o define e a etapa 4
-- (nenhum item sobreviveu ao corte), e essa decisao continua valendo.
UPDATE documento
   SET estado = 'descoberto', updated_at = now()
 WHERE estado IN ('extraido', 'suspeito', 'ilegivel', 'baixando', 'erro');
"""

DOWN = """
ALTER TABLE documento_extracao ADD COLUMN IF NOT EXISTS n_paginas integer;
ALTER TABLE documento           ADD COLUMN IF NOT EXISTS n_paginas integer;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
