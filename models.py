# models.py
from sqlalchemy import Column, Integer, String, DateTime, Date
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
