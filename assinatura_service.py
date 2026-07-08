# ===============================================================
# SERVIÇO DE ASSINATURA (regra de negócio)
# ---------------------------------------------------------------
# Orquestra Asaas + banco. NÃO guarda o cartão em lugar nenhum —
# o cartão só existe em memória durante a chamada de tokenização.
# Salva apenas: creditCardToken + asaas_customer_id + asaas_subscription_id.
# ===============================================================
import json
from datetime import datetime, date, timedelta

from asaas.asaas_client import AsaasClient, AsaasError
from models import Assinatura, Plano, AsaasEvento, Usuario


def get_plano(db, codigo):
    return db.query(Plano).filter(Plano.codigo == codigo, Plano.ativo == 1).first()


def planos_ativos(db):
    return db.query(Plano).filter(Plano.ativo == 1).order_by(Plano.valor.asc()).all()


def parse_valor(s):
    """Converte um valor digitado (com vírgula OU ponto) em float > 0.
    Ex.: '150,00' -> 150.0 ; '1.750,00' -> 1750.0 ; '1750' -> 1750.0.
    Levanta ValueError se for inválido ou <= 0."""
    txt = str(s).strip()
    try:
        if "," in txt:
            # formato brasileiro: ponto é separador de milhar, vírgula é decimal.
            valor = float(txt.replace(".", "").replace(",", "."))
        else:
            valor = float(txt)
    except (TypeError, ValueError):
        raise ValueError("Valor inválido.")
    if valor <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    return valor


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


def assinatura_do_usuario(db, user_id):
    """A assinatura MAIS RECENTE do usuário (ou None). Não cria nada."""
    return (
        db.query(Assinatura)
        .filter(Assinatura.user_id == user_id)
        .order_by(Assinatura.id.desc())
        .first()
    )


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

    # TRAVA ANTI-DUPLICAÇÃO: se já existe assinatura ATIVA na Asaas, devolve a
    # existente e NÃO cria outra (evita cobrar o cliente em dobro por duplo
    # clique / reenvio do formulário do cartão).
    if a.asaas_subscription_id and a.status == "active":
        return a

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


# ===============================================================
# AÇÕES DO PAINEL ADMIN (Fase 4)
# ---------------------------------------------------------------
# Cada ação primeiro fala com a Asaas (quando há assinatura lá) e
# SÓ DEPOIS grava no banco. Assim, se a Asaas recusar, nada muda
# localmente (o erro sobe e o painel mostra a mensagem).
# ===============================================================
def alterar_plano(db, assinatura, codigo):
    """Troca o plano da assinatura. Se já existir assinatura na Asaas,
    propaga o novo valor/ciclo (PUT subscriptions, ajustando cobranças
    em aberto)."""
    plano = get_plano(db, codigo)
    if not plano:
        raise ValueError("Plano inválido.")

    if assinatura.asaas_subscription_id:
        client = AsaasClient()
        client.atualizar_assinatura(
            assinatura.asaas_subscription_id,
            value=float(plano.valor),
            cycle=plano.ciclo,
            updatePendingPayments=True,
        )
        assinatura.last_sync = datetime.utcnow()

    assinatura.plano = plano.codigo
    assinatura.ciclo = plano.ciclo
    assinatura.valor = float(plano.valor)
    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


def alterar_valor(db, assinatura, novo_valor):
    """Muda só o VALOR (mantém o plano/ciclo). Aceita vírgula ou ponto.
    Propaga para a Asaas se houver assinatura lá."""
    valor = parse_valor(novo_valor)

    if assinatura.asaas_subscription_id:
        client = AsaasClient()
        client.atualizar_assinatura(
            assinatura.asaas_subscription_id,
            value=valor,
            updatePendingPayments=True,
        )
        assinatura.last_sync = datetime.utcnow()

    assinatura.valor = valor
    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


def cancelar(db, assinatura):
    """Cancela a assinatura na Asaas (se houver) e marca como cancelada aqui."""
    if assinatura.asaas_subscription_id:
        client = AsaasClient()
        client.cancelar_assinatura(assinatura.asaas_subscription_id)
        assinatura.last_sync = datetime.utcnow()

    assinatura.status = "cancelled"
    assinatura.cancelado_em = datetime.utcnow()
    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


def definir_controle(db, assinatura, controle, dias=None):
    """Override manual do admin:
       automatico        -> volta a seguir a Asaas
       liberado_manual   -> libera acesso (opcionalmente por X dias)
       bloqueado_manual  -> bloqueia o acesso, ignorando a Asaas"""
    if controle not in ("automatico", "liberado_manual", "bloqueado_manual"):
        raise ValueError("Controle inválido.")

    assinatura.controle = controle
    if controle == "liberado_manual" and dias:
        try:
            d = int(dias)
        except (TypeError, ValueError):
            d = 0
        assinatura.controle_ate = (datetime.utcnow() + timedelta(days=d)) if d > 0 else None
    else:
        assinatura.controle_ate = None

    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


def definir_valor_travado(db, assinatura, travado):
    """Trava/destrava o valor da assinatura. Travado (1) = o reajuste em massa
    do plano PULA este cliente (caso de promoção / preço combinado)."""
    assinatura.valor_travado = 1 if travado else 0
    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


def desvincular_asaas(db, user_id):
    """Remove o(s) registro(s) local(is) de assinatura do usuário (e os eventos
    ligados). NÃO chama a Asaas — só limpa o que está salvo AQUI. Usado para:
      - limpar dados de TESTE do sandbox antes de ir para produção (os IDs/token
        do sandbox não valem em produção);
      - deixar um cliente assinar do ZERO.
    O cliente volta a 'grandfathered' (acesso pelo status do usuário) até
    assinar de novo, quando uma assinatura nova é criada. Devolve quantas
    assinaturas foram removidas. ATENÇÃO: se houver assinatura ATIVA de verdade
    na Asaas, cancele antes (senão a Asaas continua cobrando sem o painel saber)."""
    assinaturas = db.query(Assinatura).filter(Assinatura.user_id == user_id).all()
    ids = [a.id for a in assinaturas]
    if ids:
        db.query(AsaasEvento).filter(
            AsaasEvento.assinatura_id.in_(ids)
        ).delete(synchronize_session=False)
    for a in assinaturas:
        db.delete(a)
    db.commit()
    return len(assinaturas)


# Status da ASSINATURA na Asaas -> nosso status.
_STATUS_SUB_PARA_LOCAL = {
    "ACTIVE": "active",
    "EXPIRED": "overdue",
    "INACTIVE": "cancelled",
}

# Status da COBRANÇA na Asaas -> nosso status (mais preciso que o da assinatura).
_STATUS_COBRANCA_PARA_LOCAL = {
    "CONFIRMED": "active",
    "RECEIVED": "active",
    "RECEIVED_IN_CASH": "active",
    "OVERDUE": "overdue",
    "REFUNDED": "suspended",
    "REFUND_REQUESTED": "suspended",
    "CHARGEBACK_REQUESTED": "suspended",
    "CHARGEBACK_DISPUTE": "suspended",
}


def sincronizar(db, assinatura):
    """Puxa o estado atual da Asaas (assinatura + última cobrança) e atualiza
    o banco. Use quando achar que um webhook foi perdido."""
    if not assinatura.asaas_subscription_id:
        raise ValueError("Esta assinatura ainda não tem ID na Asaas.")

    client = AsaasClient()
    sub = client.consultar_assinatura(assinatura.asaas_subscription_id)

    # Dados da assinatura.
    if sub.get("value") is not None:
        assinatura.valor = float(sub["value"])
    if sub.get("cycle"):
        assinatura.ciclo = sub["cycle"]
    if sub.get("nextDueDate"):
        assinatura.proximo_vencimento = _parse_date(sub["nextDueDate"])

    novo_status = _STATUS_SUB_PARA_LOCAL.get((sub.get("status") or "").upper())

    # Última cobrança (mais precisa p/ saber se está pago/vencido).
    try:
        cobrancas = client.listar_cobrancas_assinatura(assinatura.asaas_subscription_id)
        lista = cobrancas.get("data") or []
    except AsaasError:
        lista = []
    if lista:
        # A ordem da Asaas pode variar; pega a mais recente pela data de criação.
        lista.sort(
            key=lambda c: (c.get("dateCreated") or c.get("dueDate") or ""),
            reverse=True,
        )
        ultima = lista[0]
        assinatura.ultimo_pagamento_status = ultima.get("status")
        mapeado = _STATUS_COBRANCA_PARA_LOCAL.get((ultima.get("status") or "").upper())
        if mapeado:
            novo_status = mapeado

    if novo_status:
        assinatura.status = novo_status
    assinatura.last_sync = datetime.utcnow()
    assinatura.atualizado_em = datetime.utcnow()
    db.commit()
    return assinatura


# ===============================================================
# REAJUSTE EM MASSA (Fase 5)
# ---------------------------------------------------------------
# Muda o valor de um PLANO e propaga para TODAS as assinaturas
# ATUAIS daquele plano na Asaas (PUT /subscriptions, ajustando as
# cobranças em aberto). Cada cliente entra com a sua assinatura
# MAIS RECENTE (ignora histórico e canceladas). Falha em uma
# assinatura não derruba as outras — vira item no relatório.
# ===============================================================
def assinaturas_atuais_do_plano(db, codigo):
    """A assinatura mais recente de cada usuário cujo plano atual == codigo
    e que NÃO esteja cancelada."""
    todas = (
        db.query(Assinatura)
        .order_by(Assinatura.user_id.asc(), Assinatura.id.desc())
        .all()
    )
    atual_por_user = {}
    for a in todas:
        atual_por_user.setdefault(a.user_id, a)  # 1ª por user (id desc) = a mais recente
    return [
        a for a in atual_por_user.values()
        if a.plano == codigo and a.status != "cancelled"
    ]


def reajustar_plano(db, codigo, novo_valor):
    """Aplica o reajuste: atualiza o valor do plano e propaga para as
    assinaturas atuais. Retorna um relatório (dict) com as listas
    ok / falha / local. Levanta ValueError se o valor/plano for inválido."""
    valor = parse_valor(novo_valor)

    plano = db.query(Plano).filter(Plano.codigo == codigo).first()
    if not plano:
        raise ValueError("Plano não encontrado.")
    valor_antigo = plano.valor

    alvos = assinaturas_atuais_do_plano(db, codigo)
    user_ids = [a.user_id for a in alvos]
    usuarios = {}
    if user_ids:
        usuarios = {
            u.id: u for u in db.query(Usuario).filter(Usuario.id.in_(user_ids)).all()
        }

    client = AsaasClient()
    rel = {"ok": [], "falha": [], "local": [], "travadas": []}

    for a in alvos:
        u = usuarios.get(a.user_id)
        nome = u.nome if u else f"usuário #{a.user_id}"
        email = u.email if u else ""

        # Valor TRAVADO (promoção): pula no reajuste em massa e mantém o valor atual.
        if a.valor_travado:
            rel["travadas"].append({"nome": nome, "email": email})
            continue

        if a.asaas_subscription_id:
            try:
                client.atualizar_assinatura(
                    a.asaas_subscription_id,
                    value=valor,
                    updatePendingPayments=True,
                )
            except AsaasError as e:
                # Falhou na Asaas: NÃO mexe no valor local (continua refletindo a Asaas).
                rel["falha"].append({
                    "nome": nome, "email": email,
                    "sub_id": a.asaas_subscription_id, "erro": str(e),
                })
                continue
            a.valor = valor
            a.last_sync = datetime.utcnow()
            a.atualizado_em = datetime.utcnow()
            rel["ok"].append({
                "nome": nome, "email": email, "sub_id": a.asaas_subscription_id,
            })
        else:
            # Sem assinatura na Asaas: só atualiza o valor local.
            a.valor = valor
            a.atualizado_em = datetime.utcnow()
            rel["local"].append({"nome": nome, "email": email})

    # O valor do plano sempre é atualizado (vale para novos assinantes).
    plano.valor = valor
    plano.atualizado_em = datetime.utcnow()
    db.commit()

    rel["plano_nome"] = plano.nome
    rel["codigo"] = plano.codigo
    rel["valor_antigo"] = valor_antigo
    rel["valor_novo"] = valor
    rel["total"] = len(alvos)
    return rel


# ===============================================================
# ACESSO (status automático + override manual do admin)
# ===============================================================
def acesso_liberado(db, user):
    """Decide se o usuário tem acesso. Retorna (liberado: bool, motivo: str).
    Considera: bloqueio manual do admin, override da assinatura e o status
    automático (Asaas). Cliente SEM assinatura = grandfathered (cliente antigo)."""
    if user.status == "bloqueado":
        return False, "bloqueado_admin"

    a = (
        db.query(Assinatura)
        .filter(Assinatura.user_id == user.id)
        .order_by(Assinatura.id.desc())
        .first()
    )
    if not a:
        # cliente antigo (criado antes das assinaturas) -> segue o status do usuário
        return (user.status == "ativo"), "sem_assinatura"

    # Override manual do admin (com prazo opcional).
    if a.controle == "bloqueado_manual":
        return False, "bloqueado_manual"
    if a.controle == "liberado_manual":
        if not a.controle_ate or a.controle_ate >= datetime.utcnow():
            return True, "liberado_manual"
        # prazo do override venceu -> cai no automático abaixo

    # Automático (vindo da Asaas / webhook).
    if a.status in ("active", "trial"):
        return True, "ativo"
    return False, (a.status or "pending_payment")


# ===============================================================
# WEBHOOK (idempotente + histórico)
# ===============================================================
_EVENTO_PARA_STATUS = {
    "PAYMENT_CONFIRMED": "active",
    "PAYMENT_RECEIVED": "active",
    "PAYMENT_OVERDUE": "overdue",
    "PAYMENT_REFUNDED": "suspended",
    "PAYMENT_CHARGEBACK_REQUESTED": "suspended",
    "PAYMENT_CHARGEBACK_DISPUTE": "suspended",
    "PAYMENT_DELETED": "cancelled",
    "SUBSCRIPTION_DELETED": "cancelled",
}

_CHAVES_SENSIVEIS = ("creditCard", "creditCardToken", "creditCardHolderInfo")


def _sanitizar(obj):
    """Remove qualquer dado de cartão antes de guardar o payload do webhook."""
    if isinstance(obj, dict):
        return {k: _sanitizar(v) for k, v in obj.items() if k not in _CHAVES_SENSIVEIS}
    if isinstance(obj, list):
        return [_sanitizar(x) for x in obj]
    return obj


def processar_webhook(db, payload):
    """Processa um evento do Asaas (IDEMPOTENTE). Atualiza o status da
    assinatura e guarda o evento (sanitizado) como histórico."""
    event_id = payload.get("id")
    tipo = payload.get("event")

    # Idempotência: não processa o mesmo evento duas vezes.
    if event_id and db.query(AsaasEvento).filter(AsaasEvento.asaas_event_id == event_id).first():
        return

    pagamento = payload.get("payment") or {}
    assinatura_payload = payload.get("subscription") or {}
    sub_id = pagamento.get("subscription") or assinatura_payload.get("id")

    assinatura = None
    if sub_id:
        assinatura = db.query(Assinatura).filter(Assinatura.asaas_subscription_id == sub_id).first()

    ev = AsaasEvento(
        asaas_event_id=event_id,
        tipo=tipo,
        assinatura_id=(assinatura.id if assinatura else None),
        payload=json.dumps(_sanitizar(payload), ensure_ascii=False)[:5000],
        processado=0,
    )
    db.add(ev)

    if assinatura:
        novo_status = _EVENTO_PARA_STATUS.get(tipo)
        if novo_status:
            assinatura.status = novo_status
            assinatura.ultimo_pagamento_status = pagamento.get("status")
            assinatura.last_sync = datetime.utcnow()
            assinatura.atualizado_em = datetime.utcnow()
        ev.processado = 1

    db.commit()
