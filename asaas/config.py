# ===============================================================
# CONFIGURAÇÃO DA INTEGRAÇÃO ASAAS
# ---------------------------------------------------------------
# Tudo vem de variáveis de ambiente (Railway). NUNCA hardcode a
# chave. Sandbox e produção são 100% separados (a Asaas confirmou):
# o token de um ambiente não funciona no outro.
# ===============================================================
import os

# A chave da Asaas começa com "$" (ex.: $aact_...). O Railway interpreta o "$"
# inicial como referência de variável e ZERA o valor. Solução robusta: guarde a
# chave SEM o "$" no Railway; o código recoloca o "$" aqui automaticamente.
_chave = os.getenv("ASAAS_API_KEY", "").strip()
if _chave and not _chave.startswith("$"):
    _chave = "$" + _chave
ASAAS_API_KEY = _chave
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
