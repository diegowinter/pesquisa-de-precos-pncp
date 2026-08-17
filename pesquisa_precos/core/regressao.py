"""
Núcleo puro da suite de regressão de qualidade (Fase 9, docs/04_FASES.md).

Decide, para um rótulo com `score_rerank` conhecido, o que a 6b decidiria HOJE com os
thresholds candidatos — sem chamar LLM nenhum (a faixa ambígua da 6c fica de fora do cálculo
de precisão/recall, porque decidi-la exige o modelo; aqui só se mede o que os thresholds da 6b
sozinhos resolvem). Comparado contra `rotulo.decisao_final`, dá precisão/recall — "trocar de
threshold/modelo/prompt é no escuro" deixa de ser verdade (docs/08_CONVENCOES.md §6, suite de
regressão da Fase 9).

Mantido separado de `ferramentas/regressao.py` porque é lógica pura, sem I/O — é o que permite
testar com fixture sintética em milissegundos (critério de aceite: suite roda em <5min e
reprova threshold degradado).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Decisao = Literal["confirmado", "rejeitado", "ambiguo"]


@dataclass
class Rotulo:
    par_key: str
    score_rerank: float | None
    decisao_final: str  # 'confirmado' | 'rejeitado' | 'pendente' (docs/02_SCHEMA.md §2)


@dataclass
class ResultadoRegressao:
    n_amostra: int
    n_decididos: int          # exclui os que caíram na faixa ambígua (não é erro, é escopo da 6b)
    n_ambiguos: int
    verdadeiros_positivos: int
    falsos_positivos: int
    verdadeiros_negativos: int
    falsos_negativos: int
    precisao: float | None
    recall: float | None
    detalhe: list[dict] = field(default_factory=list)

    @property
    def aprovado(self) -> bool:
        """Sem limiar aqui de propósito — quem decide "reprova" é o chamador (script/teste),
        comparando contra o limiar que faz sentido para o caso de uso."""
        return self.precisao is not None and self.recall is not None


def decidir(score: float | None, t_aceita: float, t_rejeita: float) -> Decisao:
    """Réplica da regra de corte da etapa 6b: score ≥ t_aceita → confirmado; score < t_rejeita
    → rejeitado; entre os dois → ambíguo (vai para a 6c/LLM, fora do escopo desta suite)."""
    if score is None:
        return "ambiguo"
    if score >= t_aceita:
        return "confirmado"
    if score < t_rejeita:
        return "rejeitado"
    return "ambiguo"


def avaliar(rotulos: list[Rotulo], *, t_aceita: float, t_rejeita: float) -> ResultadoRegressao:
    """Precisão/recall dos thresholds candidatos contra `decisao_final` já rotulada.

    `pendente` é descartado da amostra: não é um rótulo de verdade (ainda não convergiu),
    então não pode servir de gabarito nem para precisão nem para recall.
    """
    vp = fp = vn = fn = 0
    ambiguos = 0
    detalhe: list[dict] = []
    considerados = [r for r in rotulos if r.decisao_final in ("confirmado", "rejeitado")]
    for r in considerados:
        predito = decidir(r.score_rerank, t_aceita, t_rejeita)
        if predito == "ambiguo":
            ambiguos += 1
            detalhe.append({"par_key": r.par_key, "score": r.score_rerank,
                            "esperado": r.decisao_final, "predito": predito, "acerto": None})
            continue
        real_positivo = r.decisao_final == "confirmado"
        pred_positivo = predito == "confirmado"
        if pred_positivo and real_positivo:
            vp += 1
        elif pred_positivo and not real_positivo:
            fp += 1
        elif not pred_positivo and not real_positivo:
            vn += 1
        else:
            fn += 1
        detalhe.append({"par_key": r.par_key, "score": r.score_rerank,
                        "esperado": r.decisao_final, "predito": predito,
                        "acerto": pred_positivo == real_positivo})

    n_decididos = vp + fp + vn + fn
    precisao = vp / (vp + fp) if (vp + fp) > 0 else None
    recall = vp / (vp + fn) if (vp + fn) > 0 else None
    return ResultadoRegressao(
        n_amostra=len(considerados), n_decididos=n_decididos, n_ambiguos=ambiguos,
        verdadeiros_positivos=vp, falsos_positivos=fp, verdadeiros_negativos=vn,
        falsos_negativos=fn, precisao=precisao, recall=recall, detalhe=detalhe,
    )
