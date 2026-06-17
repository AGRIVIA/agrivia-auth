# database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# ===============================================================
# ENDEREÇO DO BANCO
# ---------------------------------------------------------------
# Lê o endereço do banco da variável de ambiente DATABASE_URL
# (no Railway ela aponta para o Postgres).
# Se a variável não existir (ex.: rodando na sua máquina para testar),
# cai automaticamente para o SQLite local "usuarios.db".
# ===============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///usuarios.db")

# O Railway às vezes entrega o endereço começando com "postgres://",
# mas o SQLAlchemy moderno espera "postgresql://". Corrigimos aqui.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# check_same_thread só vale para SQLite; no Postgres não pode ser enviado.
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,  # evita erro de "conexão morta" no Postgres
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

# ===============================
# DEPENDÊNCIA DE SESSÃO (FastAPI)
# ===============================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
