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

    # 🔹 NOVO CAMPO: vencimento do pagamento
    vencimento_pagamento = Column(DateTime, nullable=True)

    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, nullable=True)
