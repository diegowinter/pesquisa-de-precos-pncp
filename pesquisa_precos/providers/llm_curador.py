"""
Camada de curadoria com LLM via LangChain (um provedor OpenAI-compatível).

Os prompts e as categorias ficam em `scripts/prompts.py`; aqui fica só a lógica de
chamada ao modelo e o parsing da resposta. Os nomes de prompts/categorias são
reexportados abaixo para manter compatibilidade com quem importa deste módulo.

Para cada item candidato do catálogo, decide UMA categoria:
  materiais: arma_fogo, arma_nao_letal, municao, protecao_balistica, equip_ti,
             equip_comunicacao, viatura, bicicleta, drone_rpas, outros
  serviços:  service_selecao, service_seguranca, outros
A curadoria MANTÉM itens com categoria != outros/erro (a REGRA-ZERO descarta
acessórios/peças jogando-os em "outros").
"""

import base64
import json
import re
import unicodedata

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from pesquisa_precos.core import prompts_resolver
from pesquisa_precos.core.prompts_resolver import PromptsAtivos

# Prompts e categorias vivem em prompts.py — reexportados aqui (retrocompatível).
from pesquisa_precos.core.prompts import (  # noqa: F401
    CATEGORIAS_MATERIAL,
    CATEGORIAS_SERVICO,
    CATEGORIA_MAP_MATERIAL,
    CATEGORIA_MAP_SERVICO,
    _bloco_categorias_classificacao,
    montar_prompt_busca,
    bloco_itens_candidatos,
    montar_prompt_casar_itens_tabela,
    montar_prompt_classificar_item,
    montar_prompt_comparar_item,
    montar_prompt_comparar_par,
    montar_prompt_extrair_tabela_documento,
    montar_prompt_material,
    montar_prompt_servico,
    montar_prompt_termos_item,
)
from pesquisa_precos.core.classification.categories import IDS_CONTEUDO as _IDS_CONTEUDO


def _extrair_json(texto: str) -> dict:
    """
    Faz strip de cercas markdown (```json ... ```) e devolve o objeto JSON parseado.
    Levanta json.JSONDecodeError se não houver JSON válido — quem chama faz o retry.
    """
    t = texto.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    # Recorta do primeiro '{' ao último '}' (tolera texto solto ao redor).
    ini, fim = t.find("{"), t.rfind("}")
    if ini != -1 and fim != -1 and fim > ini:
        t = t[ini:fim + 1]
    return json.loads(t)


def _sem_acento(s: str) -> str:
    """Remove acentos p/ casar a resposta do modelo (ex.: 'MUNIÇÃO') com o id ascii."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def parse_resposta(text: str, categoria_map: dict) -> tuple[str, str]:
    """Parse de 'id_categoria|justificativa' (reusa a lógica do v2/v3)."""
    if "|" in text:
        partes = text.split("|", 1)
        categoria_raw = partes[0].strip().upper()
        justificativa = partes[1].strip()
    else:
        categoria_raw = text.strip().upper().split()[0] if text.strip() else ""
        justificativa = text.strip()
    categoria_raw = _sem_acento(categoria_raw.rstrip(".,;:"))
    categoria = categoria_map.get(categoria_raw)
    if categoria:
        return categoria, justificativa
    return "erro", text[:120]


def parse_resposta_binaria(text: str) -> str:
    """Parse robusto de resposta esperada 'sim'/'nao' (aceita variações de caixa/pontuação/acento)."""
    t = _sem_acento(text.strip().lower().rstrip(".,;:! "))
    if t.startswith("sim"):
        return "sim"
    if t.startswith("nao"):
        return "nao"
    return "erro"


class Curador:
    """Encapsula o ChatOpenAI e a classificação de um item por tipo."""

    def __init__(self, model: str, base_url: str, api_key: str, temperature: float = 0.1,
                 timeout: int = 60, max_retries: int = 0, reasoning: dict | None = None,
                 extra_body: dict | None = None, prompts_ativos: PromptsAtivos | None = None):
        # max_retries: retry NATIVO do cliente OpenAI (honra Retry-After em 429/5xx).
        #   0 (default) preserva o comportamento dos scripts existentes.
        #   O script paralelo passa um valor maior (ex.: 8) para 429 robusto.
        # reasoning: objeto `reasoning` do OpenRouter, enviado via extra_body como
        #   {"reasoning": ...}. Ex.: {"enabled": False} desliga o raciocínio (OpenRouter).
        # extra_body: campos crus mesclados no corpo da requisição (top-level). Use para
        #   parâmetros específicos do servidor. Ex. LM Studio: {"reasoning_effort": "none"}
        #   desliga o think (o formato `reasoning` do OpenRouter é IGNORADO pelo LM Studio).
        # O .with_retry(...) externo continua como rede de segurança para exceções gerais.
        kwargs = dict(
            model=model, base_url=base_url, api_key=api_key,
            temperature=temperature, timeout=timeout, max_retries=max_retries,
        )
        corpo = dict(extra_body or {})
        if reasoning is not None:
            corpo["reasoning"] = reasoning
        if corpo:
            kwargs["extra_body"] = corpo
        self.llm = ChatOpenAI(**kwargs).with_retry(stop_after_attempt=3)
        # Prompts ativos resolvidos UMA vez, fora de qualquer pool de threads (ver
        # `core.prompts_resolver`) — `None`/dict vazio preserva o comportamento antigo
        # (texto hardcoded de `core/prompts.py`).
        self._prompts_ativos = prompts_ativos

    # `from_provedor(cfg, provider, forte=...)` foi REMOVIDO na Fase 14 (ADR-022): ele
    # resolvia modelo/URL/chave pelo `.env`, contornando `provider_capability` — o segundo
    # caminho que a ADR-022 existe para eliminar. Quem precisa de um Curador pede
    # `ctx.providers.novo_chat(...)`, que passa pelo resolver.

    def _invocar_json(self, prompt: str) -> dict:
        """
        Invoca o modelo esperando JSON puro. Faz 1 retry em resposta inválida (JSONDecodeError).
        Levanta a última exceção se as duas tentativas falharem — quem chama trata/loga o erro.
        """
        ultimo_erro = None
        ultima_resposta = ""
        for _ in range(2):
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
            try:
                return _extrair_json(texto)
            except (json.JSONDecodeError, ValueError) as e:
                ultimo_erro, ultima_resposta = e, texto
        # A causa do parsing sozinha ("Expecting value: line 1 column 1") diz que veio VAZIO,
        # não por que. Guardar o começo da resposta e o `finish_reason` é o que transforma a
        # próxima ocorrência em diagnóstico — `length` (estourou a saída, tipicamente reasoning
        # comendo o orçamento) e `content_filter` pedem consertos completamente diferentes.
        motivo = ""
        try:
            meta = getattr(resp, "response_metadata", None) or {}
            motivo = meta.get("finish_reason") or ""
        except Exception:  # noqa: BLE001 — diagnóstico nunca derruba a chamada
            motivo = ""
        detalhe = f" [finish_reason={motivo}]" if motivo else ""
        amostra = ultima_resposta[:300] if ultima_resposta else "(resposta vazia)"
        raise RuntimeError(f"{ultimo_erro}{detalhe} — resposta do model: {amostra}")

    def classificar_categoria(self, descricao: str, unidade: str = "") -> dict:
        """
        Etapa 3 — classifica um item PNCP em 0+ categorias de conteúdo (multi-label).
        Retorna {"categorias": [...], "confianca": "alta|media|baixa", "_prompt_versao_id": ...}.
        Filtra ids inválidos. Nunca levanta: em erro devolve
        {"categorias": [], "confianca": "erro", "_erro": msg}.

        O texto do prompt vem do banco (`prompt_version` ativa de 'classificar_item') se houver
        uma; senão cai no hardcoded de `core/prompts.py` (Fase 6, ver `prompts_resolver`). O
        bloco de categorias é sempre renderizado em código a partir de `categorias.py` — é
        dado de domínio, não texto de instrução (ADR-014).
        """
        ctx_unidade = f"\n  Unidade: {unidade}" if unidade else ""
        prompt, prompt_version_id = prompts_resolver.resolver(
            self._prompts_ativos, "classificar_item",
            montar_prompt_classificar_item(descricao, unidade),
            bloco_categorias=_bloco_categorias_classificacao(),
            descricao=descricao, ctx_unidade=ctx_unidade)
        try:
            data = self._invocar_json(prompt)
        except Exception as e:  # noqa: BLE001
            return {"categorias": [], "confianca": "erro", "_erro": str(e)[:200],
                    "_prompt_versao_id": prompt_version_id}
        cats = data.get("categorias") or []
        if isinstance(cats, str):
            cats = [cats]
        validas = [c for c in (str(x).strip().lower() for x in cats) if c in _IDS_CONTEUDO]
        return {"categorias": validas, "confianca": str(data.get("confianca", "")).lower(),
                "_prompt_versao_id": prompt_version_id}

    def extrair_tabela_documento(self, pdf_bytes: bytes, filename: str) -> str:
        """
        Etapa 5, 1ª chamada (ADR-023) — manda o PDF INTEIRO como anexo e recebe a TABELA DE
        ITENS em texto, "as it is". A saída NÃO é JSON de propósito: cada documento tem as
        colunas que tem, e um esquema fixo obrigaria o modelo a preencher campo inexistente.

        Devolve "" quando o modelo não achou tabela (responde SEM_TABELA) — quem chama trata
        isso como documento ilegível. Levanta em erro de rede/provedor: ao contrário das
        chamadas por item, esta é o gargalo caro do documento, e engolir a falha aqui foi
        o que produziu 4.159 documentos "processados" sem nenhum resultado.
        """
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        prompt, prompt_version_id = prompts_resolver.resolver(
            self._prompts_ativos, "extrair_tabela_documento",
            montar_prompt_extrair_tabela_documento())
        content = [
            {"type": "text", "text": prompt},
            # `file` é o content part do protocolo OpenAI para documento. O OpenRouter só
            # parseia o PDF se o plugin `file-parser` for pedido — ele vai no `extra_body`
            # que o `__init__` já aceita, ligado pelo Params da etapa.
            {"type": "file",
             "file": {"filename": filename,
                      "file_data": f"data:application/pdf;base64,{b64}"}},
        ]
        resp = self.llm.invoke([HumanMessage(content=content)])
        texto = (resp.content or "").strip()
        if not texto or texto.strip().upper().startswith("SEM_TABELA"):
            return ""
        return texto

    def casar_itens_tabela(self, itens_api: list[dict], tabela_texto: str) -> dict[int, dict]:
        """
        Etapa 5, 2ª chamada (ADR-024) — casa os candidatos de UMA compra contra a tabela de UM
        documento, numa chamada só. Devolve `numero_item -> {descricao_completa,
        preco_unitario, quantidade, fornecedor}`, contendo APENAS os que o modelo achou.

        Candidato ausente do retorno significa "não está neste documento", que é o caso comum
        e correto — um pregão gera várias atas e cada uma registra o que um fornecedor ganhou.

        LEVANTA em erro de chamada, ao contrário da versão por item que devolvia
        `encontrado=False`. Falha de rede/modelo e "o item não está aqui" produziam o mesmo
        `nao_encontrado`, e foi assim que 4.159 documentos passaram por erro silencioso.
        Quem chama distingue os dois casos e grava `status='erro'` com a causa.
        """
        prompt, prompt_version_id = prompts_resolver.resolver(
            self._prompts_ativos, "casar_itens_tabela",
            montar_prompt_casar_itens_tabela(itens_api, tabela_texto),
            tabela_texto=tabela_texto, itens_fmt=bloco_itens_candidatos(itens_api))
        data = self._invocar_json(prompt)
        out: dict[int, dict] = {}
        for achado in (data.get("itens") or []):
            if not isinstance(achado, dict):
                continue
            try:
                numero = int(achado.get("numero_item"))
            except (TypeError, ValueError):
                continue
            out[numero] = {
                "encontrado": True,
                "descricao_completa": achado.get("descricao_completa") or "",
                "preco_unitario": achado.get("preco_unitario"),
                "quantidade": achado.get("quantidade"),
                "fornecedor": str(achado.get("fornecedor") or "").strip(),
                "_prompt_versao_id": prompt_version_id,
            }
        return out

    def comparar_par(self, texto_catalogo: str, texto_item: str) -> dict:
        """
        Etapa 6c — decide se catálogo e item PNCP são o mesmo item.
        Retorna {"mesmo_item": "sim|nao", "justificativa": str, "_prompt_versao_id": ...}.
        Nunca levanta: em erro devolve {"mesmo_item":"erro","justificativa":msg}.
        """
        prompt, prompt_version_id = prompts_resolver.resolver(
            self._prompts_ativos, "comparar_par",
            montar_prompt_comparar_par(texto_catalogo, texto_item),
            texto_catalogo=texto_catalogo, texto_item=texto_item)
        try:
            data = self._invocar_json(prompt)
        except Exception as e:  # noqa: BLE001
            return {"mesmo_item": "erro", "justificativa": str(e)[:200],
                    "_prompt_versao_id": prompt_version_id}
        mesmo = _sem_acento(str(data.get("mesmo_item", "")).strip().lower())
        mesmo = "sim" if mesmo.startswith("sim") else ("nao" if mesmo.startswith("nao") else "erro")
        return {"mesmo_item": mesmo, "justificativa": str(data.get("justificativa", ""))[:200],
                "_prompt_versao_id": prompt_version_id}

    def gerar_termos_item(self, name: str, descricao: str, tipo: str = "material",
                          nome_grupo: str = "") -> list[str]:
        """Etapa 1 (nova) — termos de busca genéricos direto de UM item. [] em erro."""
        prompt = montar_prompt_termos_item(name, descricao, tipo, nome_grupo)
        try:
            data = self._invocar_json(prompt)
        except Exception:  # noqa: BLE001
            return []
        termos = data.get("termos") or []
        return [str(t).strip().lower() for t in termos if str(t).strip()]

    def _prompt_e_map(self, row: dict, tipo: str, com_justificativa: bool):
        if tipo == "material":
            return montar_prompt_material(row, com_justificativa), CATEGORIA_MAP_MATERIAL
        return montar_prompt_servico(row, com_justificativa), CATEGORIA_MAP_SERVICO

    def classificar(self, row: dict, tipo: str, com_justificativa: bool = True) -> tuple[str, str]:
        """Retorna (categoria, justificativa) para um item de 'material' ou 'servico'."""
        prompt, categoria_map = self._prompt_e_map(row, tipo, com_justificativa)
        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
        except Exception as e:  # falha permanente após retries
            return "erro", str(e)[:120]
        return parse_resposta(texto, categoria_map)

    def gerar_termo_busca(self, name: str, descricao: str, categoria: str = "") -> str:
        """
        Gera um termo de busca otimizado p/ o PNCP a partir de um item do catálogo já curado.
        Nunca levanta: em erro ou resposta vazia, retorna "" (quem chama cai no fallback
        do `termo_busca`/`name` cru do catálogo).
        """
        prompt = montar_prompt_busca(name, descricao, categoria)
        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
        except Exception:
            return ""
        return texto.strip(' "\'\n\t')

    def comparar_item(self, descricao_pncp: str, objeto_compra: str, nome_catalogo: str, descricao_catalogo: str) -> str:
        """
        Compara um item de contrato/ata (PNCP) com o item de catálogo que originou a busca.
        Retorna 'sim', 'nao' ou 'erro'. Nunca levanta.
        """
        prompt = montar_prompt_comparar_item(descricao_pncp, objeto_compra, nome_catalogo, descricao_catalogo)
        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
        except Exception:
            return "erro"
        return parse_resposta_binaria(texto)

    def classificar_lote(
        self, rows: list[dict], tipo: str, com_justificativa: bool = True, max_concurrency: int = 5
    ) -> list[tuple[str, str]]:
        """Classifica vários itens em paralelo (ChatOpenAI.batch). Preserva a ordem de `rows`."""
        if not rows:
            return []
        prompts = []
        categoria_map = None
        for row in rows:
            prompt, categoria_map = self._prompt_e_map(row, tipo, com_justificativa)
            prompts.append([HumanMessage(content=prompt)])
        try:
            respostas = self.llm.batch(prompts, config={"max_concurrency": max_concurrency})
        except Exception as e:
            return [("erro", str(e)[:120])] * len(rows)
        out = []
        for resp in respostas:
            texto = (getattr(resp, "content", "") or "").strip()
            out.append(parse_resposta(texto, categoria_map))
        return out
