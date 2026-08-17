"""
m01 — Config e provedores iniciais: `.env` → `config_versao`, `config_valor`, `provedor`.

Cria a versão de config "migrada do .env" com os valores que a pipeline usa HOJE, para que os
runs migrados apontem para uma configuração concreta. Sem isso, `run.config_versao_id NOT NULL`
não teria a que se prender e o m03 não conseguiria criar o run do acervo.

O que vai para o banco é o que muda a RESPOSTA (thresholds, min_itens, top_n, modelos, URLs) —
ADR-014. O que muda o MÉTODO fica no código. `min_itens=1` e `top_n=0` entram como estão no
`.env`, que é a "regra dos 5" desativada (ADR-016), não um valor a corrigir.

CHAVE DE API NÃO VAI PARA O BANCO (§5.10). `provedor.api_key_ref` guarda o NOME da variável de
ambiente; o valor continua no `.env`, fora do git.

Uso: python -m migracao.m01_config_inicial
"""

from pesquisa_precos.config.settings import carregar_config
from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import execucao as repo
from migracao._comum import Relatorio, cabecalho, console

ROTULO = "migrada do .env"

# Chave de config → chave do dict de `carregar_config()`. Só o que a interface poderá editar
# depois (Fase 6); credencial e caminho de arquivo ficam de fora de propósito.
CHAVES = (
    "rejeitor_threshold", "rerank_t_aceita", "rerank_t_rejeita", "min_itens", "top_n",
    "embedder_model", "reranker_model",
    "custo_usd_chamada_pass1", "custo_usd_chamada_pass2",
)


def migrar() -> Relatorio:
    rel = Relatorio("m01 — config e provedores")
    cfg = carregar_config()

    with db.sessao() as s:
        cv = repo.config_versao_por_rotulo(s, ROTULO)
        if cv is None:
            cv = repo.criar_config_versao(
                s, ROTULO, criado_por="migracao",
                notas="Snapshot do .env no corte da Fase 2. Config é imutável (ADR-014): "
                      "qualquer ajuste posterior deve criar uma versão nova.")
            rel.mais("config_versao criada")
        else:
            rel.mais("config_versao reaproveitada")

        valores = {c: cfg[c] for c in CHAVES}
        # A "regra dos 5" está desativada e isso é intencional (ADR-016) — registrado como
        # valor, para que a interface mostre 0 = sem teto em vez de alguém "consertar".
        rel.mais("config_valor", repo.gravar_config(s, cv, valores))

        # Provedores: um por natureza de acesso. `capacidades` reflete o que cada um atende
        # hoje na prática, não o que poderia atender.
        repo.upsert_provedor(
            s, "openrouter", ["chat"], cfg["openrouter_base_url"],
            api_key_ref="OPENAI_API_KEY", modelo_padrao=cfg["model_pass1"],
            permite_fallback=True)
        repo.upsert_provedor(
            s, "lm_studio", ["chat"], cfg["local_base_url"],
            api_key_ref="LOCAL_API_KEY", modelo_padrao=cfg["local_model"],
            permite_fallback=True)
        repo.upsert_provedor(
            s, "gpu_caseira", ["embed", "rerank"], cfg["gpu_base_url"],
            api_key_ref="GPU_API_KEY", modelo_padrao=cfg["embedder_model"],
            permite_fallback=False)  # embed NUNCA cai para outro provedor (ADR-006 §2)
        repo.upsert_provedor(
            s, "ocr_local", ["ocr"], cfg["ocr_base_url"],
            api_key_ref="OCR_API_KEY", modelo_padrao=cfg["ocr_model"],
            permite_fallback=True)
        rel.mais("provedor", 4)

        # `chat` aponta para o LM Studio: o modelo barato é a política, não uma flag
        # (ADR-004). Trocar para openrouter é decisão explícita, com teto de custo junto.
        repo.apontar_capacidade(s, "chat", "lm_studio", cfg["local_model"],
                                fallback="openrouter")
        repo.apontar_capacidade(s, "embed", "gpu_caseira", cfg["embedder_model"])
        repo.apontar_capacidade(s, "rerank", "gpu_caseira", cfg["reranker_model"],
                                fallback=None)
        repo.apontar_capacidade(s, "ocr", "ocr_local", cfg["ocr_model"])
        rel.mais("capacidade_provedor", 4)

        if not cfg["local_model"]:
            rel.aviso("LOCAL_MODEL vazio no .env — capacidade 'chat' ficou sem modelo padrão.")
    return rel


def main() -> None:
    cabecalho("m01 — config inicial", [], "config_versao, config_valor, provedor")
    console.print(f"  banco  : {db.url_banco()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
