"""
Camada de curadoria com LLM via LangChain (um provider OpenAI-compatível).

Os prompts e as categorias ficam em `scripts/prompts.py`; aqui fica só a lógica de
chamada ao modelo e o parsing da resposta. Os nomes de prompts/categorias são
reexportados abaixo para manter compatibilidade com quem importa deste módulo.

Para cada item candidato do catálogo, decide UMA categoria:
  materiais: arma_fogo, arma_nao_letal, municao, protecao_balistica, equip_ti,
             equip_comunicacao, viatura, bicicleta, drone_rpas, outros
  serviços:  servico_selecao, servico_seguranca, outros
A curadoria MANTÉM itens com categoria != outros/erro (a REGRA-ZERO descarta
acessórios/peças jogando-os em "outros").
"""

import base64
import json
import re
import unicodedata

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

# Prompts e categorias vivem em prompts.py — reexportados aqui (retrocompatível).
from scripts.prompts import (  # noqa: F401
    CATEGORIAS_MATERIAL,
    CATEGORIAS_SERVICO,
    CATEGORIA_MAP_MATERIAL,
    CATEGORIA_MAP_SERVICO,
    montar_prompt_busca,
    montar_prompt_casar_item_tabela,
    montar_prompt_classificar_item,
    montar_prompt_comparar_item,
    montar_prompt_comparar_par,
    montar_prompt_extrair_item_pdf,
    montar_prompt_extrair_itens,
    montar_prompt_extrair_tabela_pdf,
    montar_prompt_material,
    montar_prompt_servico,
    montar_prompt_termos_item,
)
from scripts.categorias import IDS_CONTEUDO as _IDS_CONTEUDO


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
                 extra_body: dict | None = None):
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

    @classmethod
    def from_provedor(cls, cfg: dict, provedor: str, forte: bool = False, **kwargs) -> "Curador":
        """Constrói um Curador resolvendo model/base_url/api_key via config.resolver_provedor."""
        from scripts.config import resolver_provedor
        p = resolver_provedor(cfg, provedor, forte=forte)
        return cls(model=p["model"], base_url=p["base_url"], api_key=p["api_key"], **kwargs)

    def _invocar_json(self, prompt: str) -> dict:
        """
        Invoca o modelo esperando JSON puro. Faz 1 retry em resposta inválida (JSONDecodeError).
        Levanta a última exceção se as duas tentativas falharem — quem chama trata/loga o erro.
        """
        ultimo_erro = None
        for _ in range(2):
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
            try:
                return _extrair_json(texto)
            except (json.JSONDecodeError, ValueError) as e:
                ultimo_erro = e
        raise ultimo_erro if ultimo_erro else RuntimeError("resposta vazia")

    def classificar_categoria(self, descricao: str, unidade: str = "") -> dict:
        """
        Etapa 3 — classifica um item PNCP em 0+ categorias de conteúdo (multi-label).
        Retorna {"categorias": [...], "confianca": "alta|media|baixa"}. Filtra ids inválidos.
        Nunca levanta: em erro devolve {"categorias": [], "confianca": "erro", "_erro": msg}.
        """
        prompt = montar_prompt_classificar_item(descricao, unidade)
        try:
            data = self._invocar_json(prompt)
        except Exception as e:  # noqa: BLE001
            return {"categorias": [], "confianca": "erro", "_erro": str(e)[:200]}
        cats = data.get("categorias") or []
        if isinstance(cats, str):
            cats = [cats]
        validas = [c for c in (str(x).strip().lower() for x in cats) if c in _IDS_CONTEUDO]
        return {"categorias": validas, "confianca": str(data.get("confianca", "")).lower()}

    def extrair_item_pdf(self, janela_texto: str, item_api: dict) -> dict:
        """
        Etapa 5.2 — extração guiada de UM item do texto do PDF.
        Retorna {"descricao_completa","preco_unitario","quantidade","encontrado"}.
        Nunca levanta: em erro devolve encontrado=False + "_erro".
        """
        prompt = montar_prompt_extrair_item_pdf(janela_texto, item_api)
        try:
            data = self._invocar_json(prompt)
        except Exception as e:  # noqa: BLE001
            return {"encontrado": False, "_erro": str(e)[:200]}
        return {
            "descricao_completa": data.get("descricao_completa") or "",
            "preco_unitario": data.get("preco_unitario"),
            "quantidade": data.get("quantidade"),
            "encontrado": bool(data.get("encontrado")),
        }

    def extrair_tabela_pdf(self, png_bytes: bytes) -> list[dict]:
        """
        Etapa 5_alt_a — envia a IMAGEM de UMA página a um modelo de VISÃO e recebe a
        tabela de itens da página, "as it is" (lista de linhas estruturadas). Uma imagem
        por chamada (nunca o doc inteiro). Nunca levanta: em erro devolve [].
        """
        b64 = base64.b64encode(png_bytes).decode("ascii")
        content = [
            {"type": "text", "text": montar_prompt_extrair_tabela_pdf()},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        try:
            resp = self.llm.invoke([HumanMessage(content=content)])
            data = _extrair_json((resp.content or "").strip())
        except Exception:  # noqa: BLE001
            return []
        itens = data.get("itens") or []
        out = []
        for it in itens:
            if not isinstance(it, dict):
                continue
            out.append({k: str(it.get(k, "") or "").strip() for k in
                        ("numero_item", "descricao", "unidade", "quantidade",
                         "preco_unitario", "preco_total", "fornecedor")})
        return out

    def casar_item_tabela(self, item_api: dict, linhas: list[dict]) -> dict:
        """
        Etapa 5_alt_b — casa UM item da API contra a tabela LIMPA (linhas de 5_alt_a).
        Retorna {"encontrado","idx","descricao_completa","preco_unitario","quantidade"}.
        Nunca levanta: em erro devolve encontrado=False, idx=-1.
        """
        prompt = montar_prompt_casar_item_tabela(item_api, linhas)
        try:
            data = self._invocar_json(prompt)
        except Exception:  # noqa: BLE001
            return {"encontrado": False, "idx": -1}
        try:
            idx = int(data.get("idx", -1))
        except (TypeError, ValueError):
            idx = -1
        return {
            "encontrado": bool(data.get("encontrado")),
            "idx": idx,
            "descricao_completa": data.get("descricao_completa") or "",
            "preco_unitario": data.get("preco_unitario"),
            "quantidade": data.get("quantidade"),
        }

    def comparar_par(self, texto_catalogo: str, texto_item: str) -> dict:
        """
        Etapa 6c — decide se catálogo e item PNCP são o mesmo item.
        Retorna {"mesmo_item": "sim|nao", "justificativa": str}.
        Nunca levanta: em erro devolve {"mesmo_item":"erro","justificativa":msg}.
        """
        prompt = montar_prompt_comparar_par(texto_catalogo, texto_item)
        try:
            data = self._invocar_json(prompt)
        except Exception as e:  # noqa: BLE001
            return {"mesmo_item": "erro", "justificativa": str(e)[:200]}
        mesmo = _sem_acento(str(data.get("mesmo_item", "")).strip().lower())
        mesmo = "sim" if mesmo.startswith("sim") else ("nao" if mesmo.startswith("nao") else "erro")
        return {"mesmo_item": mesmo, "justificativa": str(data.get("justificativa", ""))[:200]}

    def gerar_termos_item(self, nome: str, descricao: str, tipo: str = "material",
                          nome_grupo: str = "") -> list[str]:
        """Etapa 1 (nova) — termos de busca genéricos direto de UM item. [] em erro."""
        prompt = montar_prompt_termos_item(nome, descricao, tipo, nome_grupo)
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

    def gerar_termo_busca(self, nome: str, descricao: str, categoria: str = "") -> str:
        """
        Gera um termo de busca otimizado p/ o PNCP a partir de um item do catálogo já curado.
        Nunca levanta: em erro ou resposta vazia, retorna "" (quem chama cai no fallback
        do `termo_busca`/`nome` cru do catálogo).
        """
        prompt = montar_prompt_busca(nome, descricao, categoria)
        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto = (resp.content or "").strip()
        except Exception:
            return ""
        return texto.strip(' "\'\n\t')

    def extrair_itens(self, texto: str) -> str:
        """
        Extrai os itens de uma ata (texto corrido de `parsear_pdfs.py`) usando o modelo de
        visão/extração (tipicamente um modelo pequeno, ex. 7B). Nunca levanta: em erro,
        retorna "" (quem chama decide o fallback/status).
        """
        prompt = montar_prompt_extrair_itens(texto)
        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            texto_resp = (resp.content or "").strip()
        except Exception:
            return ""
        return texto_resp

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
