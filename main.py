import os
import sqlite3
import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from starlette.middleware.sessions import SessionMiddleware

from admin.admin_web import router as admin_web_router
from admin.admin_routes import router as admin_router
from sync_routes import router as sync_router

from database import SessionLocal, engine
from models import Base, Usuario, AceiteTermos
from auth import verify_password, create_access_token, hash_password
from termos_config import TERMOS_URL, POLITICA_URL, TERMOS_VERSAO, POLITICA_VERSAO


# Endereço público do servidor (usado no link de confirmação de e-mail).
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://agrivia-auth-production.up.railway.app"
).rstrip("/")

# Liga/desliga a EXIGÊNCIA de aceite dos termos NO LOGIN (re-aceite quando a
# versão dos documentos muda). DESLIGADO por padrão pra não trancar clientes
# atuais sem decisão sua. Para ligar: defina EXIGIR_ACEITE_LOGIN=1 no Railway.
EXIGIR_ACEITE_LOGIN = os.getenv("EXIGIR_ACEITE_LOGIN", "0") == "1"


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

# 📁 ARQUIVOS ESTÁTICOS (logo e imagens do painel admin)
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ===============================
# DATABASE
# ===============================
Base.metadata.create_all(bind=engine)


# ===============================================================
# SUBIDA B — MIGRAÇÃO: COLUNAS DE VALIDAÇÃO DE E-MAIL
# ---------------------------------------------------------------
# Adiciona as colunas de validação na tabela 'usuarios' (se ainda
# não existirem) e, na PRIMEIRA vez que a coluna email_verificado
# é criada, marca TODAS as contas que já existiam como confirmadas
# (grandfather) — assim ninguém que já usa o sistema é trancado.
# Roda uma vez; depois não faz mais nada.
# ===============================================================
def migrar_colunas_email():
    insp = inspect(engine)
    existentes = [c["name"] for c in insp.get_columns("usuarios")]

    adicionou_verificado = "email_verificado" not in existentes

    novas = []
    if "email_verificado" not in existentes:
        novas.append("ALTER TABLE usuarios ADD COLUMN email_verificado INTEGER DEFAULT 0")
    if "token_confirmacao" not in existentes:
        novas.append("ALTER TABLE usuarios ADD COLUMN token_confirmacao VARCHAR")
    if "token_expira" not in existentes:
        novas.append("ALTER TABLE usuarios ADD COLUMN token_expira TIMESTAMP")

    if not novas:
        return

    with engine.begin() as conn:
        for sql in novas:
            conn.execute(text(sql))
            print("[migracao]", sql)
        if adicionou_verificado:
            conn.execute(text("UPDATE usuarios SET email_verificado = 1"))
            print("[migracao] contas existentes marcadas como e-mail confirmado (grandfather).")


# ===============================================================
# PASSO 1.3 — CONTAS NO BANCO PRINCIPAL (Postgres)
# ---------------------------------------------------------------
# 1) Garante o ADMIN a partir de variáveis SECRETAS do Railway
#    (ADMIN_EMAIL / ADMIN_PASSWORD).
# 2) Copia os CLIENTES (não-admin) do arquivo antigo 'usuarios.db'
#    preservando a senha — só na primeira vez (banco vazio de
#    clientes). Admin e clientes copiados entram já confirmados.
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
                    email_verificado=1,
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
                        continue  # não duplica
                    db.add(Usuario(
                        nome=r["nome"],
                        email=email,
                        senha_hash=r["senha_hash"],
                        status=(r["status"] if "status" in cols else "ativo"),
                        is_admin=0,
                        email_verificado=1,  # clientes que já existiam entram confirmados
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


# Ordem importa: cria tabela -> adiciona colunas/grandfather -> garante contas.
migrar_colunas_email()
seed_inicial()

# ===============================
# ROUTERS
# ===============================
app.include_router(admin_web_router)
app.include_router(admin_router)
app.include_router(sync_router)

# ===============================
# DEPENDÊNCIA DB
# ===============================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ===============================================================
# PÁGINA SIMPLES (confirmação de e-mail) — visual AGRIVIA
# ===============================================================
def _pagina_html(titulo: str, mensagem: str) -> str:
    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:40px; max-width:460px; text-align:center;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">{titulo}</h1>
    <p style="font-size:15px; line-height:1.5;">{mensagem}</p>
  </div>
</body></html>"""


# ===============================================================
# HELPERS DO ACEITE
# ===============================================================
def _client_ip(request: Request) -> str:
    # Atrás do proxy do Railway, o IP real vem no cabeçalho X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _aceite_atual_valido(db: Session, user_id: int) -> bool:
    """True se o usuário já aceitou a versão ATUAL dos dois documentos."""
    return db.query(AceiteTermos).filter(
        AceiteTermos.user_id == user_id,
        AceiteTermos.termos_versao == TERMOS_VERSAO,
        AceiteTermos.politica_versao == POLITICA_VERSAO,
    ).first() is not None


def _pagina_aceite(token: str, email: str, erro: str = None) -> str:
    erro_html = ""
    if erro:
        erro_html = (
            '<p style="background:rgba(192,57,43,0.15); border:1px solid #c0392b; '
            'color:#e7897d; padding:10px 12px; border-radius:8px; font-size:14px;">'
            f'{erro}</p>'
        )
    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ativar conta - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:520px; width:100%;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">Ative sua conta AGRIVIA</h1>
    <p style="font-size:15px; line-height:1.5;">Conta: <b>{email}</b></p>
    <p style="font-size:15px; line-height:1.5;">Para concluir a ativação, leia e aceite os documentos abaixo.</p>
    {erro_html}
    <form method="post" action="/confirmar">
      <input type="hidden" name="token" value="{token}">
      <label style="display:flex; gap:10px; align-items:flex-start; font-size:14px; line-height:1.5; background:#0c1826; border:1px solid #2b3a22; border-radius:8px; padding:14px; cursor:pointer;">
        <input type="checkbox" name="aceite" value="1" style="margin-top:3px; width:18px; height:18px;">
        <span>Li e aceito os
          <a href="{TERMOS_URL}" target="_blank" rel="noopener" style="color:#9fc35a;">Termos de Uso</a>
          e a
          <a href="{POLITICA_URL}" target="_blank" rel="noopener" style="color:#9fc35a;">Política de Privacidade</a>
          (Versão {TERMOS_VERSAO}).
        </span>
      </label>
      <button type="submit" style="margin-top:22px; width:100%; background:#476126; color:#fff; border:none; border-radius:8px; padding:13px; font-size:15px; font-weight:bold; cursor:pointer;">
        Ativar minha conta
      </button>
    </form>
  </div>
</body></html>"""


# ===============================================================
# CONFIRMAÇÃO DE E-MAIL + ACEITE DOS TERMOS
# ===============================================================
@app.get("/confirmar", response_class=HTMLResponse)
def confirmar_get(token: str, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.token_confirmacao == token).first()
    if not user:
        return HTMLResponse(
            _pagina_html("Link inválido", "Este link não é válido. Fale com o suporte."),
            status_code=400
        )
    if user.token_expira and user.token_expira < datetime.utcnow():
        return HTMLResponse(
            _pagina_html("Link expirado", "Este link expirou. Peça um novo ao suporte."),
            status_code=400
        )
    return HTMLResponse(_pagina_aceite(token, user.email))


@app.post("/confirmar", response_class=HTMLResponse)
def confirmar_post(
    request: Request,
    token: str = Form(...),
    aceite: str = Form(None),
    db: Session = Depends(get_db),
):
    user = db.query(Usuario).filter(Usuario.token_confirmacao == token).first()
    if not user:
        return HTMLResponse(
            _pagina_html("Link inválido", "Este link não é válido. Fale com o suporte."),
            status_code=400
        )
    if user.token_expira and user.token_expira < datetime.utcnow():
        return HTMLResponse(
            _pagina_html("Link expirado", "Este link expirou. Peça um novo ao suporte."),
            status_code=400
        )

    # ACEITE OBRIGATÓRIO: sem marcar, não ativa.
    if not aceite:
        return HTMLResponse(
            _pagina_aceite(
                token, user.email,
                erro="Você precisa marcar o aceite dos Termos de Uso e da Política de Privacidade para continuar."
            ),
            status_code=400
        )

    # Ativa a conta + GRAVA A PROVA do aceite (trilha de auditoria).
    user.email_verificado = 1
    user.token_confirmacao = None
    user.token_expira = None
    db.add(AceiteTermos(
        user_id=user.id,
        email=user.email,
        termos_versao=TERMOS_VERSAO,
        politica_versao=POLITICA_VERSAO,
        ip=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    ))
    db.commit()

    return HTMLResponse(
        _pagina_html("Conta ativada!", "Pronto! Você aceitou os termos e sua conta foi ativada. Já pode entrar no AGRIVIA.")
    )


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

    if not user.email_verificado:
        raise HTTPException(
            status_code=403,
            detail="E-mail não confirmado. Verifique seu e-mail para ativar a conta."
        )

    # (Re)ACEITE DOS TERMOS por versão — só atua se a exigência estiver LIGADA.
    if EXIGIR_ACEITE_LOGIN and not user.is_admin and not _aceite_atual_valido(db, user.id):
        novo_token = secrets.token_urlsafe(32)
        user.token_confirmacao = novo_token
        user.token_expira = datetime.utcnow() + timedelta(days=3)
        db.commit()
        link = f"{PUBLIC_BASE_URL}/confirmar?token={novo_token}"
        raise HTTPException(
            status_code=403,
            detail=("É necessário aceitar os Termos de Uso e a Política de Privacidade "
                    f"atualizados. Abra este link no navegador para aceitar: {link}")
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
