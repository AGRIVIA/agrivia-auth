# settings.py
from datetime import timedelta

# ===============================
# BANCO DE DADOS
# ===============================
DATABASE_URL = "sqlite:///usuarios.db"
# depois você troca por PostgreSQL sem quebrar nada

# ===============================
# SEGURANÇA / TOKEN
# ===============================
SECRET_KEY = "MUDE_ESSA_CHAVE_PARA_ALGO_SEGURO"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

ACCESS_TOKEN_EXPIRE = timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
