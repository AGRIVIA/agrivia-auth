# ===============================================================
# SERVIÇO DE ASSINATURA (regra de negócio)
# ---------------------------------------------------------------
# Orquestra Asaas + banco. NÃO guarda o cartão em lugar nenhum —
# o cartão só existe em memória durante a chamada de tokenização.
# Salva apenas: creditCardToken + asaas_customer_id + asaas_subscription_id.
# ===============================================================
from datetime import datetime, date

from asaas.asaas_client import AsaasClient, AsaasError
from models import Assinatura, Plano


def get_plano(db, codigo):
    return db.query(Plano).filter(Plano.codigo == codigo, Plano.ativo == 1).first()


def planos_ativos(db):
    return db.query(Plano).filter(Plano.ativo == 1).order_by(Plano.valor.asc()).all()


def get_or_create_assinatura(db, user):
    a = (
        db.query(Assinatura)
        .filter(Assinatura.user_id == user.id)
        .order_by(Assinatura.id.desc())
        .first()
    )
    if not a:
        a = Assinatura(user_id=user.id, status="pending_payment", controle="automatico")
        db.add(a)
        db.commit()
        db.refresh(a)
    return a


def definir_plano(db, user, codigo):
    plano = get_plano(db, codigo)
    if not plano:
        raise ValueError("Plano inválido.")
    a = get_or_create_assinatura(db, user)
    a.plano = plano.codigo
    a.ciclo = plano.ciclo
    a.valor = plano.valor
    a.atualizado_em = datetime.utcnow()
    db.commit()
    return a


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


def criar_assinatura_completa(db, user, cartao, titular, remote_ip):
    """Cria cliente na Asaas, tokeniza o cartão, cria a assinatura e libera o
    acesso. O 'cartao' (dict) é descartado ao fim — nunca é salvo/logado.
    Levanta AsaasError/ValueError em caso de problema (mensagens seguras)."""
    a = get_or_create_assinatura(db, user)
    if not a.plano:
        raise ValueError("Escolha um plano antes de informar o cartão.")
    plano = get_plano(db, a.plano)
    if not plano:
        raise ValueError("Plano não encontrado.")

    client = AsaasClient()

    # 1) Cliente na Asaas (reutiliza se já existir).
    if not a.asaas_customer_id:
        cli = client.criar_cliente(
            nome=titular.get("name") or user.nome,
            email=user.email,
            cpf_cnpj=titular.get("cpfCnpj"),
            telefone=titular.get("phone"),
        )
        a.asaas_customer_id = cli.get("id")
        db.commit()

    # 2) Tokeniza o cartão (só aqui o cartão existe; depois some).
    tk = client.tokenizar_cartao(a.asaas_customer_id, cartao, titular, remote_ip)
    token = tk.get("creditCardToken")
    if not token:
        raise AsaasError("Não foi possível tokenizar o cartão. Confira os dados e tente de novo.")
    a.credit_card_token = token   # salva SÓ o token (nunca o cartão)
    db.commit()

    # 3) Cria a assinatura usando SOMENTE o token.
    sub = client.criar_assinatura(
        customer_id=a.asaas_customer_id,
        credit_card_token=token,
        valor=float(plano.valor),
        ciclo=plano.ciclo,
        proximo_vencimento=date.today().isoformat(),
        descricao=f"AGRIVIA - Plano {plano.nome}",
    )

    # 4) Salva os IDs e libera o acesso.
    a.asaas_subscription_id = sub.get("id")
    a.status = "active"
    a.proximo_vencimento = _parse_date(sub.get("nextDueDate"))
    a.last_sync = datetime.utcnow()
    a.atualizado_em = datetime.utcnow()

    user.status = "ativo"
    user.email_verificado = 1
    user.token_confirmacao = None
    user.token_expira = None

    db.commit()
    return a
