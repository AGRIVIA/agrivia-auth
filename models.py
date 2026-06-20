# models.py
from sqlalchemy import Column, Integer, String, DateTime, Date, LargeBinary, ForeignKey
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
