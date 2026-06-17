import os
import sqlite3
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from starlette.middleware.sessions import SessionMiddleware

from admin.admin_web import router as admin_web_router
from admin.admin_routes import router as admin_router

from database import SessionLocal, engine
from models import Base, Usuario
from auth import verify_password, create_access_token, hash_password


# ===============================
# APP
# ===============================
app = FastAPI(title="AGRIVIA Auth API")

# 🔓 CORS (OBRIGATÓRIO)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 SESSION (ADMIN WEB)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "AGRIVIA_ADMIN_SESSION_KEY_2026")
)

# ===============================
# DATABASE
# ===============================
Base.metadata.create_all(bind=engine)


# ===============================================================
# PASSO 1.3 — CONTAS NO BANCO PRINCIPAL (Postgres)
# ---------------------------------------------------------------
# 1) Garante o ADMIN a partir de variáveis SECRETAS do Railway
#    (ADMIN_EMAIL / ADMIN_PASSWORD) — assim a senha do admin NÃO
#    fica escrita dentro do código. Cria só se ainda não existir.
# 2) Copia os CLIENTES (não-admin) do arquivo antigo 'usuarios.db'
#    preservando a senha deles — só na primeira vez (banco vazio
#    de clientes). O admin antigo é ignorado.
# É idempotente: rodar de novo não duplica nada.
# ===============================================================
def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def seed_inicial():
    db = SessionLocal()
    try:
        # ---- 1) ADMIN (via variáveis de ambiente) ----
        admin_email = os.getenv("ADMIN_EMAIL")
        admin_senha = os.getenv("ADMIN_PASSWORD")
        if admin_email and admin_senha:
            ja_existe = db.query(Usuario).filter(Usuario.email == admin_email).first()
            if not ja_existe:
                db.add(Usuario(
                    nome="Administrador AGRIVIA",
                    email=admin_email,
                    senha_hash=hash_password(admin_senha),
                    status="ativo",
                    is_admin=1,
                ))
                db.commit()
                print(f"[seed] admin criado: {admin_email}")
            else:
                print(f"[seed] admin ja existe: {admin_email}")
        else:
            print("[seed] ADMIN_EMAIL/ADMIN_PASSWORD nao definidos; admin nao criado.")

        # ---- 2) CLIENTES (copia do usuarios.db antigo, só se ainda não houver) ----
        ja_tem_clientes = db.query(Usuario).filter(Usuario.is_admin == 0).count() > 0
        if not ja_tem_clientes:
            sqlite_path = os.path.join(os.path.dirname(__file__), "usuarios.db")
            if os.path.exists(sqlite_path):
                con = sqlite3.connect(sqlite_path)
                con.row_factory = sqlite3.Row
                try:
                    rows = con.execute(
                        "SELECT * FROM usuarios WHERE COALESCE(is_admin, 0) = 0"
                    ).fetchall()
                finally:
                    con.close()

                copiados = 0
                for r in rows:
                    cols = r.keys()
                    email = r["email"]
                    if db.query(Usuario).filter(Usuario.email == email).first():
                        continue  # não duplica (ex.: se o admin novo tiver o mesmo e-mail)
                    db.add(Usuario(
                        nome=r["nome"],
                        email=email,
                        senha_hash=r["senha_hash"],
                        status=(r["status"] if "status" in cols else "ativo"),
                        is_admin=0,
                        vencimento_pagamento=(
                            _parse_dt(r["vencimento_pagamento"])
                            if "vencimento_pagamento" in cols else None
                        ),
                    ))
                    copiados += 1
                db.commit()
                print(f"[seed] {copiados} cliente(s) copiado(s) do usuarios.db antigo.")
            else:
                print("[seed] usuarios.db antigo nao encontrado; nenhum cliente copiado.")
        else:
            print("[seed] ja existem clientes no banco; copia ignorada.")
    except Exception as e:
        db.rollback()
        print("[seed] ERRO no seed inicial:", e)
    finally:
        db.close()


seed_inicial()

# ===============================
# ROUTERS
# ===============================
app.include_router(admin_web_router)
app.include_router(admin_router)

# ===============================
# DEPENDÊNCIA DB
# ===============================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ===============================
# SCHEMA LOGIN (DESKTOP)
# ===============================
class LoginRequest(BaseModel):
    email: str
    senha: str

# ===============================
# LOGIN DESKTOP
# ===============================
@app.post("/api/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.email == data.email).first()

    if not user or not verify_password(data.senha, user.senha_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")

    if user.status != "ativo":
        raise HTTPException(
            status_code=403,
            detail=f"Usuário {user.status}. Contate o suporte."
        )

    token = create_access_token({
        "sub": user.email,
        "user_id": user.id,
        "status": user.status
    })

    return {
        "success": True,
        "token": token,
        "status": user.status
    }
