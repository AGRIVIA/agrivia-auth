# models.py
from sqlalchemy import Column, Integer, String, DateTime, Date, LargeBinary, ForeignKey, Float
from datetime import datetime
from database import Base

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    senha_hash = Column(String, nullable=False)

    status = Column(String, default="ativo")
    is_admin = Column(Integer, default=0)

    # 🔹 vencimento do pagamento
    vencimento_pagamento = Column(DateTime, nullable=True)

    # 🔹 VALIDAÇÃO DE E-MAIL (Subida B)
    email_verificado = Column(Integer, default=0)      # 0 = não confirmado | 1 = confirmado
    token_confirmacao = Column(String, nullable=True)  # token do link de confirmação
    token_expira = Column(DateTime, nullable=True)     # até quando o link vale

    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, nullable=True)

    # 🔹 TELEMETRIA LEVE (mostrada no painel admin)
    app_versao = Column(String, nullable=True)       # versão do app desktop no último login
    ultimo_acesso = Column(DateTime, nullable=True)  # data/hora do último login


# ===============================================================
# FASE 2 — CÓPIAS DO BANCO NA NUVEM (snapshots)
# ---------------------------------------------------------------
# Cada linha é uma cópia completa do banco SQLite do cliente
# (compactada em gzip), guardada por usuário e por número de
# versão. Mantemos um histórico das últimas cópias para poder
# "voltar" a uma versão anterior se necessário.
# ===============================================================
class DbSnapshot(Base):
    __tablename__ = "db_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)
    version = Column(Integer, nullable=False)        # 1, 2, 3, ... por usuário
    conteudo = Column(LargeBinary, nullable=False)   # o banco compactado (gzip)
    tamanho_bytes = Column(Integer)                  # tamanho da cópia
    device_id = Column(String, nullable=True)        # qual aparelho enviou
    criado_em = Column(DateTime, default=datetime.utcnow)


# ===============================================================
# SEGURANÇA — REGISTRO DE APARELHOS POR CONTA (anti-compartilhamento)
# ---------------------------------------------------------------
# Cada linha = um aparelho (device_id) visto usando uma conta, com
# quando foi visto pela 1ª/última vez e quantas vezes. Serve para
# DETECTAR (e avisar no painel) quando uma mesma conta aparece em
# vários aparelhos — sinal de possível compartilhamento.
# ===============================================================
class DeviceAtividade(Base):
    __tablename__ = "device_atividade"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)
    device_id = Column(String, index=True)
    primeiro_em = Column(DateTime, default=datetime.utcnow)
    ultimo_em = Column(DateTime, default=datetime.utcnow)
    acessos = Column(Integer, default=1)


# ===============================================================
# JURÍDICO — TRILHA DE AUDITORIA DE ACEITE DOS TERMOS
# ---------------------------------------------------------------
# Cada linha = UM aceite (prova). Guarda QUEM aceitou, QUAIS
# documentos e em QUAL versão, QUANDO, de QUAL IP e navegador.
# Tabela de HISTÓRICO: quando a versão dos documentos mudar e o
# cliente aceitar de novo, entra uma nova linha (a antiga fica).
# É a prova jurídica do aceite eletrônico.
# ===============================================================
class AceiteTermos(Base):
    __tablename__ = "aceites_termos"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)
    email = Column(String)                    # redundante de propósito (prova)
    termos_versao = Column(String)            # ex.: "1.0"
    politica_versao = Column(String)          # ex.: "1.0"
    aceito_em = Column(DateTime, default=datetime.utcnow)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)


# ===============================================================
# ASSINATURAS (Asaas) — PLANO
# ---------------------------------------------------------------
# Valor de cada plano. Editável pelo painel admin; o reajuste
# propaga para a Asaas (PUT /subscriptions). Ciclo = MONTHLY /
# SEMIANNUALLY / YEARLY.
# ===============================================================
class Plano(Base):
    __tablename__ = "planos"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String, unique=True, index=True)   # mensal / semestral / anual
    nome = Column(String)
    ciclo = Column(String)                             # MONTHLY / SEMIANNUALLY / YEARLY
    valor = Column(Float)
    ativo = Column(Integer, default=1)
    atualizado_em = Column(DateTime, default=datetime.utcnow)


# ===============================================================
# ASSINATURAS (Asaas) — ASSINATURA DO CLIENTE
# ---------------------------------------------------------------
# Guarda SÓ o token + os IDs da Asaas (NUNCA o cartão).
#   status   = situação automática vinda da Asaas/webhook
#   controle = override manual do admin:
#              automatico | liberado_manual | bloqueado_manual
# ===============================================================
class Assinatura(Base):
    __tablename__ = "assinaturas"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)

    asaas_customer_id = Column(String, index=True, nullable=True)
    asaas_subscription_id = Column(String, index=True, nullable=True)
    credit_card_token = Column(String, nullable=True)   # SÓ o token

    plano = Column(String, nullable=True)               # mensal / semestral / anual
    ciclo = Column(String, nullable=True)
    valor = Column(Float, nullable=True)
    valor_travado = Column(Integer, default=0)          # 1 = pular no reajuste em massa (promoção/preço combinado)
    trial_dias = Column(Integer, default=0)             # 🎁 dias grátis PENDENTES (30 = 1ª cobrança em D+30); consumido (volta a 0) quando a assinatura é criada

    status = Column(String, default="pending_payment")  # trial/active/pending_payment/overdue/cancelled/suspended
    controle = Column(String, default="automatico")     # automatico | liberado_manual | bloqueado_manual
    controle_ate = Column(DateTime, nullable=True)       # p/ liberação manual com prazo (teste de negociação)

    proximo_vencimento = Column(DateTime, nullable=True)
    ultimo_pagamento_status = Column(String, nullable=True)
    last_sync = Column(DateTime, nullable=True)

    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, nullable=True)
    cancelado_em = Column(DateTime, nullable=True)


# ===============================================================
# ASSINATURAS (Asaas) — EVENTOS / WEBHOOK (idempotência + histórico)
# ---------------------------------------------------------------
# Cada webhook recebido vira uma linha. asaas_event_id evita
# processar o mesmo evento duas vezes. payload guardado SANITIZADO.
# ===============================================================
class AsaasEvento(Base):
    __tablename__ = "asaas_eventos"

    id = Column(Integer, primary_key=True, index=True)
    asaas_event_id = Column(String, index=True, nullable=True)  # idempotência
    tipo = Column(String, nullable=True)                        # PAYMENT_CONFIRMED, etc.
    assinatura_id = Column(Integer, nullable=True)
    payload = Column(String, nullable=True)                     # JSON sanitizado (sem cartão)
    processado = Column(Integer, default=0)
    recebido_em = Column(DateTime, default=datetime.utcnow)
