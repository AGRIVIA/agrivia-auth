# ===============================================================
# CONFIGURAÇÃO DA INTEGRAÇÃO ASAAS
# ---------------------------------------------------------------
# Tudo vem de variáveis de ambiente (Railway). NUNCA hardcode a
# chave. Sandbox e produção são 100% separados (a Asaas confirmou):
# o token de um ambiente não funciona no outro.
# ===============================================================
import os

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
ASAAS_ENVIRONMENT = os.getenv("ASAAS_ENVIRONMENT", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")

# Base URL: usa ASAAS_BASE_URL se você definir; senão deduz pelo ambiente.
_BASE_PADRAO = {
    "sandbox": "https://sandbox.asaas.com/api/v3",
    "producao": "https://api.asaas.com/v3",
    "production": "https://api.asaas.com/v3",
}
ASAAS_BASE_URL = (
    os.getenv("ASAAS_BASE_URL")
    or _BASE_PADRAO.get(ASAAS_ENVIRONMENT, _BASE_PADRAO["sandbox"])
).rstrip("/")


def configurado() -> bool:
    """True se a chave da Asaas está definida (módulo pronto pra usar)."""
    return bool(ASAAS_API_KEY)
