# ===============================================================
# CLIENTE HTTP DA ASAAS (camada de infraestrutura)
# ---------------------------------------------------------------
# SÓ fala com a Asaas. Sem regra de negócio aqui (isso fica no
# assinatura_service). Payloads VALIDADOS no sandbox em 29/06/2026.
#
# SEGURANÇA:
#  - Nunca loga corpo da requisição (cartão), nem a API key.
#  - Loga apenas método + caminho + status.
#  - Retry simples em timeout/erro de servidor (5xx).
# ===============================================================
import time
import logging

import requests

from asaas import config

logger = logging.getLogger("asaas")


class AsaasError(Exception):
    """Erro vindo da Asaas (com a(s) mensagem(ns) seguras, sem cartão)."""
    def __init__(self, mensagem, status=None, erros=None):
        super().__init__(mensagem)
        self.status = status
        self.erros = erros or []


class AsaasClient:
    def __init__(self, api_key=None, base_url=None, timeout=30):
        self.api_key = api_key or config.ASAAS_API_KEY
        self.base_url = (base_url or config.ASAAS_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _headers(self):
        return {
            "access_token": self.api_key,
            "User-Agent": "agrivia-auth",
            "Content-Type": "application/json",
        }

    def _request(self, method, path, json=None, params=None, _tentativa=1):
        if not self.api_key:
            raise AsaasError(
                "Integração Asaas não configurada no servidor (ASAAS_API_KEY ausente). "
                "Verifique as variáveis no Railway."
            )
        url = self.base_url + path
        try:
            resp = requests.request(
                method, url,
                headers=self._headers(),
                json=json, params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException:
            if _tentativa < 3:
                time.sleep(0.8 * _tentativa)
                return self._request(method, path, json=json, params=params, _tentativa=_tentativa + 1)
            logger.warning("[asaas] falha de conexao em %s %s", method, path)
            raise AsaasError("Falha de conexão com a Asaas. Tente novamente.")

        # Retry em erro de servidor (5xx).
        if resp.status_code >= 500 and _tentativa < 3:
            time.sleep(0.8 * _tentativa)
            return self._request(method, path, json=json, params=params, _tentativa=_tentativa + 1)

        # Log seguro: nunca o corpo (que pode ter cartão), só status.
        logger.info("[asaas] %s %s -> %s", method, path, resp.status_code)

        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}

        erros = []
        try:
            erros = resp.json().get("errors", [])
        except Exception:
            pass
        msg = "; ".join(e.get("description", "") for e in erros if e.get("description")) \
            or f"Erro na Asaas (HTTP {resp.status_code})"
        raise AsaasError(msg, status=resp.status_code, erros=erros)

    # ---------------- CLIENTES ----------------
    def criar_cliente(self, nome, email, cpf_cnpj, telefone=None):
        # notificationDisabled=True: a Asaas NÃO manda nenhuma mensagem ao
        # cliente (SMS/WhatsApp geram TAXA; quem fala com o cliente é o
        # próprio AGRIVIA, por e-mail). Cobrança é automática no cartão,
        # então o cliente não precisa de lembrete da Asaas.
        body = {
            "name": nome,
            "email": email,
            "cpfCnpj": cpf_cnpj,
            "notificationDisabled": True,
        }
        if telefone:
            body["mobilePhone"] = telefone
        return self._request("POST", "/customers", json=body)

    def consultar_cliente(self, customer_id):
        return self._request("GET", f"/customers/{customer_id}")

    def atualizar_cliente(self, customer_id, **campos):
        return self._request("POST", f"/customers/{customer_id}", json=campos)

    # ---------------- TOKENIZAÇÃO ----------------
    def tokenizar_cartao(self, customer_id, cartao, titular, remote_ip):
        """Tokeniza o cartão e devolve {creditCardToken, creditCardBrand, creditCardNumber}.
        cartao  = {holderName, number, expiryMonth, expiryYear, ccv}
        titular = {name, email, cpfCnpj, postalCode, addressNumber, phone}
        remote_ip = IP do CLIENTE (não do servidor).
        O cartão NUNCA é logado nem guardado."""
        body = {
            "customer": customer_id,
            "creditCard": cartao,
            "creditCardHolderInfo": titular,
            "remoteIp": remote_ip,
        }
        return self._request("POST", "/creditCard/tokenizeCreditCard", json=body)

    # ---------------- ASSINATURAS ----------------
    def criar_assinatura(self, customer_id, credit_card_token, valor, ciclo, proximo_vencimento, descricao):
        body = {
            "customer": customer_id,
            "billingType": "CREDIT_CARD",
            "creditCardToken": credit_card_token,
            "value": valor,
            "cycle": ciclo,                       # MONTHLY / SEMIANNUALLY / YEARLY
            "nextDueDate": proximo_vencimento,    # "AAAA-MM-DD"
            "description": descricao,
        }
        return self._request("POST", "/subscriptions", json=body)

    def atualizar_assinatura(self, subscription_id, **campos):
        """Reajuste: muda value / nextDueDate / cycle mantendo o MESMO cartão tokenizado.
        Aceita updatePendingPayments=True p/ propagar o novo valor às cobranças
        ainda em aberto."""
        return self._request("PUT", f"/subscriptions/{subscription_id}", json=campos)

    def cancelar_assinatura(self, subscription_id):
        return self._request("DELETE", f"/subscriptions/{subscription_id}")

    def consultar_assinatura(self, subscription_id):
        return self._request("GET", f"/subscriptions/{subscription_id}")

    def listar_cobrancas_assinatura(self, subscription_id, limit=20):
        """Lista as cobranças (pagamentos) geradas por uma assinatura.
        Devolve {'data': [...]}. Usado pelo 'Sincronizar' do painel admin
        para descobrir o status real (pago / vencido / etc.)."""
        return self._request(
            "GET",
            f"/subscriptions/{subscription_id}/payments",
            params={"limit": limit},
        )

    def consultar_cobranca(self, payment_id):
        return self._request("GET", f"/payments/{payment_id}")
