import os
import secrets
from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Usuario
from auth import verify_password, hash_password
from email_service import enviar_confirmacao
from datetime import date, datetime, timedelta

# -------------------------------------------------
# ROUTER
# -------------------------------------------------
router = APIRouter(prefix="/admin", tags=["Admin Web"])

# Caminho ABSOLUTO para a pasta de templates, baseado na localização deste
# arquivo. Assim funciona em qualquer lugar (servidor ou sua máquina),
# independente de "de qual pasta" o servidor foi iniciado.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


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
        request,
        "login.html",
        {"error": None}
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
            request,
            "login.html",
            {"error": "Email ou senha inválidos"}
        )

    if not user.is_admin:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Acesso restrito ao administrador"}
        )

    request.session["admin_id"] = user.id

    return RedirectResponse("/admin/usuarios", status_code=302)


# -------------------------------------------------
# LISTAR USUÁRIOS
# -------------------------------------------------
@router.get("/usuarios", response_class=HTMLResponse)
def listar_usuarios(
    request: Request,
    busca: str = "",
    filtro: str = "",
    ok: str = "",
    erro: str = "",
    db: Session = Depends(get_db)
):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return RedirectResponse("/admin/login", status_code=302)

    usuarios = db.query(Usuario).order_by(Usuario.id).all()
    hoje = date.today()
    termo = (busca or "").strip().lower()

    # Contadores SEMPRE sobre o total (não sobre o que está filtrado na tela).
    total = len(usuarios)
    n_ativos = 0
    n_bloqueados = 0
    n_vencidos = 0
    n_nao_confirmados = 0

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

        confirmado = bool(u.email_verificado)

        # ----- contadores -----
        if u.status == "ativo":
            n_ativos += 1
        if u.status == "bloqueado":
            n_bloqueados += 1
        if venc_status == "vencido":
            n_vencidos += 1
        if not confirmado:
            n_nao_confirmados += 1

        # ----- busca por nome/e-mail -----
        if termo and termo not in (u.nome or "").lower() and termo not in (u.email or "").lower():
            continue

        # ----- filtro rápido (cards) -----
        if filtro == "ativos" and u.status != "ativo":
            continue
        if filtro == "bloqueados" and u.status != "bloqueado":
            continue
        if filtro == "vencidos" and venc_status != "vencido":
            continue
        if filtro == "nao_confirmados" and confirmado:
            continue

        usuarios_view.append({
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "status": u.status,
            "is_admin": u.is_admin,
            "confirmado": confirmado,
            "vencimento": venc_data.strftime("%d/%m/%Y") if venc_data else None,
            "vencimento_status": venc_status
        })

    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {
            "usuarios": usuarios_view,
            "admin_atual_id": admin_id,
            "busca": busca or "",
            "filtro": filtro or "",
            "ok": ok or "",
            "erro": erro or "",
            "contadores": {
                "total": total,
                "ativos": n_ativos,
                "bloqueados": n_bloqueados,
                "vencidos": n_vencidos,
                "nao_confirmados": n_nao_confirmados,
            },
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

# -------------------------------------------------
# REENVIAR E-MAIL DE CONFIRMAÇÃO
# -------------------------------------------------
@router.post("/usuarios/{user_id}/reenviar-confirmacao")
def reenviar_confirmacao_action(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if usuario.email_verificado:
        return RedirectResponse("/admin/usuarios?erro=ja_confirmado", status_code=302)

    # Gera um novo token (validade 3 dias) e reenvia o e-mail.
    token = secrets.token_urlsafe(32)
    usuario.token_confirmacao = token
    usuario.token_expira = datetime.utcnow() + timedelta(days=3)
    db.commit()

    base = os.getenv(
        "PUBLIC_BASE_URL",
        "https://agrivia-auth-production.up.railway.app"
    ).rstrip("/")
    link = f"{base}/confirmar?token={token}"

    enviado = enviar_confirmacao(usuario.email, usuario.nome, link)
    if enviado:
        return RedirectResponse("/admin/usuarios?ok=reenviado", status_code=302)
    return RedirectResponse("/admin/usuarios?erro=envio", status_code=302)

# -------------------------------------------------
# EXCLUIR USUÁRIO
# -------------------------------------------------
@router.post("/usuarios/{user_id}/excluir")
def excluir_usuario_action(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    # Segurança: o admin não pode excluir a própria conta (evita ficar trancado fora).
    if user_id == admin.id:
        return RedirectResponse("/admin/usuarios?erro=auto_exclusao", status_code=302)

    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    db.delete(usuario)
    db.commit()
    return RedirectResponse("/admin/usuarios?ok=excluido", status_code=302)

@router.get("/usuarios/novo", response_class=HTMLResponse)
def criar_usuario_page(
    request: Request,
    admin: Usuario = Depends(admin_session_required)
):
    return templates.TemplateResponse(
        request,
        "criar_usuario.html",
        {"error": None}
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
            request,
            "criar_usuario.html",
            {
                "error": "Email já cadastrado"
            }
        )

    eh_admin = 1 if is_admin else 0

    novo = Usuario(
        nome=nome,
        email=email,
        senha_hash=hash_password(senha),
        status=status,
        is_admin=eh_admin,
    )

    # Admin não precisa confirmar e-mail; cliente precisa.
    if eh_admin:
        novo.email_verificado = 1
        db.add(novo)
        db.commit()
        return RedirectResponse(url="/admin/usuarios", status_code=302)

    # Cliente: cria como NÃO confirmado e gera o link de confirmação.
    token = secrets.token_urlsafe(32)
    novo.email_verificado = 0
    novo.token_confirmacao = token
    novo.token_expira = datetime.utcnow() + timedelta(days=3)
    db.add(novo)
    db.commit()

    base = os.getenv(
        "PUBLIC_BASE_URL",
        "https://agrivia-auth-production.up.railway.app"
    ).rstrip("/")
    link = f"{base}/confirmar?token={token}"
    print(f"[cadastro] link de confirmacao para {email}: {link}")

    # SUBIDA B2: envia o link de confirmação por e-mail automático (Resend).
    enviado = enviar_confirmacao(email, nome, link)

    if enviado:
        pagina = f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Usuário criado - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:560px;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">Usuário criado &#9989;</h1>
    <p style="font-size:15px; line-height:1.5;">A conta de <b>{email}</b> foi criada e o <b>e-mail de confirmação foi enviado automaticamente</b>.</p>
    <p style="font-size:15px; line-height:1.5;">O cliente precisa abrir o e-mail e clicar em <b>"Confirmar meu e-mail"</b> para ativar a conta e poder entrar no AGRIVIA.</p>
    <p style="font-size:13px; line-height:1.5; color:#8fa97a;">Se o cliente não encontrar, peça para olhar a caixa de <b>spam/lixo eletrônico</b>.</p>
    <p style="margin-top:24px;"><a href="/admin/usuarios" style="background:#476126; color:#fff; text-decoration:none; padding:10px 18px; border-radius:8px; font-size:14px;">Voltar para a lista</a></p>
  </div>
</body></html>"""
        return HTMLResponse(pagina)

    # Rede de segurança: se o e-mail NÃO saiu, mostra o link na tela
    # para você enviar manualmente. A conta foi criada normalmente.
    print(f"[cadastro] e-mail NAO enviado; use o link manual para {email}: {link}")
    pagina = f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Usuário criado - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:560px;">
    <h1 style="color:#e0a93f; font-size:22px; margin-top:0;">Usuário criado &#9888;&#65039;</h1>
    <p style="font-size:15px; line-height:1.5;">A conta de <b>{email}</b> foi criada, mas o <b>e-mail automático não pôde ser enviado agora</b>.</p>
    <p style="font-size:15px; line-height:1.5;"><b>Copie o link abaixo e envie ao cliente</b> para ele confirmar o e-mail:</p>
    <div style="background:#0c1826; border:1px solid #2b3a22; border-radius:8px; padding:14px; word-break:break-all; font-size:13px; color:#bcd49a;">{link}</div>
    <p style="margin-top:24px;"><a href="/admin/usuarios" style="background:#476126; color:#fff; text-decoration:none; padding:10px 18px; border-radius:8px; font-size:14px;">Voltar para a lista</a></p>
  </div>
</body></html>"""
    return HTMLResponse(pagina)

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
        request,
        "reset_senha.html",
        {
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
        request,
        "editar_vencimento.html",
        {
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
