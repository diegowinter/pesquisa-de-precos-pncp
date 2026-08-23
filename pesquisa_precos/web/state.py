"""Ícones e rótulos de `status_etapa` (docs/06_API_E_WEB.md §4.1) — usados pelos templates do
grafo e da tela de etapa. Central para não espalhar o mapeamento em cada `.html`."""

ICONE_STEP = {
    "nao_iniciada": "○",
    "executando": "▶",
    "aguardando_aprovacao": "⏸",
    "concluida": "✓",
    "desatualizada": "⚠",
    "falhou": "✗",
    "cancelada": "⊘",
    "pulada": "⊘",
}

CLASSE_STEP = {
    "nao_iniciada": "nao-iniciada",
    "executando": "executando",
    "aguardando_aprovacao": "gate",
    "concluida": "concluida",
    "desatualizada": "desatualizada",
    "falhou": "falhou",
    "cancelada": "cancelada",
    "pulada": "cancelada",
}
