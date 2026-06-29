# ===============================================================
# PLANOS PADRÃO (valores iniciais)
# ---------------------------------------------------------------
# Estes são só os valores INICIAIS para criar a tabela `planos` na
# primeira vez. Depois você edita os valores pelo PAINEL ADMIN
# (e o reajuste propaga para a Asaas). Ciclos da Asaas:
# MONTHLY (mensal), SEMIANNUALLY (semestral), YEARLY (anual).
# ===============================================================
PLANOS_PADRAO = {
    "mensal":    {"nome": "Mensal",    "ciclo": "MONTHLY",      "valor": 187.00},
    "semestral": {"nome": "Semestral", "ciclo": "SEMIANNUALLY", "valor": 900.00},
    "anual":     {"nome": "Anual",     "ciclo": "YEARLY",       "valor": 1750.00},
}
