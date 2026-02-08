from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Usuario
from auth import verify_password, hash_password
from datetime import date

# -------------------------------------------------
# ROUTER
# -------------------------------------------------
router = APIRouter(prefix="/admin", tags=["Admin Web"])
templates = Jinja2Templates(directory="admin/templates")


# -------------------------------------------------
# DB
# -------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------------------------------
# 🔐 DEPENDÊNCIA: ADMIN LOGADO (SESSION)
# -------------------------------------------------
def admin_session_required(
    request: Request,
    db: Session = Depends(get_db)
) -> Usuario:
    admin_id = request.session.get("admin_id")

    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado"
        )

    admin = db.query(Usuario).filter(Usuario.id == admin_id).first()

    if not admin or not admin.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito ao administrador"
        )

    return admin


# -------------------------------------------------
# LOGIN (GET)
# -------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None}
    )


# -------------------------------------------------
# LOGIN (POST)
# -------------------------------------------------
@router.post("/login")
def login_action(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(Usuario).filter(Usuario.email == email).first()

    if not user or not verify_password(senha, user.senha_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email ou senha inválidos"}
        )

    if not user.is_admin:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Acesso restrito ao administrador"}
        )

    request.session["admin_id"] = user.id

    return RedirectResponse("/admin/usuarios", status_code=302)


# -------------------------------------------------
# LISTAR USUÁRIOS
# -------------------------------------------------
@router.get("/usuarios", response_class=HTMLResponse)
def listar_usuarios(
    request: Request,
    db: Session = Depends(get_db)
):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return RedirectResponse("/admin/login", status_code=302)

    usuarios = db.query(Usuario).all()
    hoje = date.today()

    usuarios_view = []

    for u in usuarios:
        venc_status = None
        venc_data = None

        if u.vencimento_pagamento:
            venc_data = u.vencimento_pagamento.date()
            dias = (venc_data - hoje).days

            if dias < 0:
                venc_status = "vencido"
            elif dias <= 7:
                venc_status = "alerta"
            else:
                venc_status = "ok"

        usuarios_view.append({
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "status": u.status,
            "is_admin": u.is_admin,
            "vencimento": venc_data.strftime("%d/%m/%Y") if venc_data else None,
            "vencimento_status": venc_status
        })

    return templates.TemplateResponse(
        "usuarios.html",
        {
            "request": request,
            "usuarios": usuarios_view
        }
    )

# -------------------------------------------------
# ALTERAR STATUS
# -------------------------------------------------
@router.post("/usuarios/{user_id}/status")
def alterar_status_usuario(
    user_id: int,
    novo_status: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if novo_status not in ["ativo", "bloqueado"]:
        raise HTTPException(status_code=400, detail="Status inválido")

    usuario.status = novo_status
    db.commit()

    return RedirectResponse("/admin/usuarios", status_code=302)

@router.get("/usuarios/novo", response_class=HTMLResponse)
def criar_usuario_page(
    request: Request,
    admin: Usuario = Depends(admin_session_required)
):
    return templates.TemplateResponse(
        "criar_usuario.html",
        {"request": request, "error": None}
    )

@router.post("/usuarios/novo")
def criar_usuario_action(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    status: str = Form(...),
    is_admin: str = Form(None),
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    existe = db.query(Usuario).filter(Usuario.email == email).first()
    if existe:
        return templates.TemplateResponse(
            "criar_usuario.html",
            {
                "request": request,
                "error": "Email já cadastrado"
            }
        )

    novo = Usuario(
        nome=nome,
        email=email,
        senha_hash=hash_password(senha),
        status=status,
        is_admin=1 if is_admin else 0
    )

    db.add(novo)
    db.commit()

    return RedirectResponse(
        url="/admin/usuarios",
        status_code=302
    )

@router.get("/usuarios/{user_id}/reset-senha", response_class=HTMLResponse)
def reset_senha_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    return templates.TemplateResponse(
        "reset_senha.html",
        {
            "request": request,
            "usuario": usuario,
            "error": None
        }
    )

@router.post("/usuarios/{user_id}/reset-senha")
def reset_senha_action(
    user_id: int,
    request: Request,
    nova_senha: str = Form(...),
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    usuario.senha_hash = hash_password(nova_senha)
    db.commit()

    return RedirectResponse(
        url="/admin/usuarios",
        status_code=302
    )

# -------------------------
# LOGOUT (ADMIN)
# -------------------------
@router.get("/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)

@router.get("/usuarios/{user_id}/vencimento", response_class=HTMLResponse)
def editar_vencimento_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    # 🔒 Proteção: admin logado
    if not request.session.get("admin_id"):
        return RedirectResponse("/admin/login", status_code=302)

    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    return templates.TemplateResponse(
        "editar_vencimento.html",
        {
            "request": request,
            "usuario": usuario
        }
    )

@router.post("/usuarios/{user_id}/vencimento")
def salvar_vencimento(
    user_id: int,
    request: Request,
    vencimento: str = Form(...),
    db: Session = Depends(get_db)
):
    # 🔒 Proteção: admin logado
    if not request.session.get("admin_id"):
        raise HTTPException(status_code=401, detail="Não autorizado")

    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    # Converte string yyyy-mm-dd → date
    usuario.vencimento_pagamento = date.fromisoformat(vencimento)

    db.commit()

    return RedirectResponse(
        url="/admin/usuarios",
        status_code=302
    )