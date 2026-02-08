from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import date

from starlette.middleware.sessions import SessionMiddleware

from admin.admin_web import router as admin_web_router
from admin.admin_routes import router as admin_router

from database import SessionLocal, engine
from models import Base, Usuario
from auth import verify_password, create_access_token


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
    secret_key="AGRIVIA_ADMIN_SESSION_KEY_2026"
)

# ===============================
# DATABASE
# ===============================
Base.metadata.create_all(bind=engine)

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

