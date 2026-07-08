import os
import io
import csv
import secrets
from urllib.parse import quote
from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Usuario, DbSnapshot, DeviceAtividade, AceiteTermos, Plano, Assinatura, AsaasEvento
from auth import verify_password, hash_password
from email_service import enviar_confirmacao, enviar_link_assinatura, enviar_link_nova_senha
from termos_config import TERMOS_VERSAO, POLITICA_VERSAO
from asaas.asaas_client import AsaasError
import assinatura_service
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

# Endereço público do servidor (para montar os links de assinatura).
_BASE_PADRAO = "https://agrivia-auth-production.up.railway.app"

# Fuso de Brasília (UTC-3, sem horário de verão) para exibir os horários.
_BR_OFFSET = timedelta(hours=3)

# Rótulos amigáveis (texto + classe de badge do base.html) por status/controle.
_STATUS_LABEL = {
    "active":          ("Ativa", "b-green"),
    "trial":           ("Teste", "b-green"),
    "pending_payment": ("Pagamento pendente", "b-amber"),
    "overdue":         ("Vencida", "b-red"),
    "suspended":       ("Suspensa", "b-red"),
    "cancelled":       ("Cancelada", "b-muted"),
}
_CONTROLE_LABEL = {
    "automatico":       ("Automático", "b-muted"),
    "liberado_manual":  ("Liberado manual", "b-green"),
    "bloqueado_manual": ("Bloqueado manual", "b-red"),
}
_CICLO_LABEL = {
    "MONTHLY": "Mensal",
    "SEMIANNUALLY": "Semestral",
    "YEARLY": "Anual",
}


def _fmt_money(v):
    if v is None:
        return "—"
    s = f"R$ {float(v):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_data(dt):
    return dt.strftime("%d/%m/%Y") if dt else "—"


def _fmt_dt_br(dt):
    """Mostra um horário guardado em UTC no fuso de Brasília."""
    return (dt - _BR_OFFSET).strftime("%d/%m/%Y %H:%M") if dt else "—"


def _status_view(a):
    if not a or not a.status:
        return ("Sem assinatura", "b-muted")
    return _STATUS_LABEL.get(a.status, (a.status, "b-muted"))


def _controle_view(a):
    if not a:
        return ("—", "b-muted")
    return _CONTROLE_LABEL.get(a.controle, (a.controle or "—", "b-muted"))


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

    # Anti-compartilhamento: aparelhos vistos por conta nos últimos 14 dias.
    limite_disp = datetime.utcnow() - timedelta(days=14)
    regs_disp = (
        db.query(DeviceAtividade)
        .filter(DeviceAtividade.ultimo_em >= limite_disp)
        .all()
    )
    dispositivos_por_user = {}
    for r in regs_disp:
        dispositivos_por_user.setdefault(r.user_id, []).append(r)

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

        devs = dispositivos_por_user.get(u.id, [])
        devs_ordenados = sorted(
            devs, key=lambda x: x.ultimo_em or datetime.min, reverse=True
        )
        devs_detalhe = " | ".join(
            f"{(d.device_id or '?')[:8]}… (últ. uso {d.ultimo_em.strftime('%d/%m %H:%M')})"
            for d in devs_ordenados
        )

        usuarios_view.append({
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "status": u.status,
            "is_admin": u.is_admin,
            "confirmado": confirmado,
            "n_dispositivos": len(devs),
            "dispositivos_detalhe": devs_detalhe,
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

    base = os.getenv("PUBLIC_BASE_URL", _BASE_PADRAO).rstrip("/")
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

    # Apaga primeiro as cópias do banco na nuvem e os registros de aparelho
    # deste usuário (senão o banco recusa excluir por causa dos vínculos).
    db.query(DbSnapshot).filter(DbSnapshot.user_id == usuario.id).delete()
    db.query(DeviceAtividade).filter(DeviceAtividade.user_id == usuario.id).delete()
    db.query(AceiteTermos).filter(AceiteTermos.user_id == usuario.id).delete()
    db.query(Assinatura).filter(Assinatura.user_id == usuario.id).delete()
    db.delete(usuario)
    db.commit()
    return RedirectResponse("/admin/usuarios?ok=excluido", status_code=302)


# -------------------------------------------------
# ACEITES DOS TERMOS (comprovante / trilha de auditoria)
# -------------------------------------------------
@router.get("/usuarios/{user_id}/aceites", response_class=HTMLResponse)
def ver_aceites(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    aceites = (
        db.query(AceiteTermos)
        .filter(AceiteTermos.user_id == user_id)
        .order_by(AceiteTermos.aceito_em.desc())
        .all()
    )

    # Mostra no horário de Brasília (UTC-3). O Brasil não tem mais horário de verão.
    for a in aceites:
        a.data_br = (
            (a.aceito_em - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M:%S")
            if a.aceito_em else "—"
        )

    aceitou_versao_atual = any(
        a.termos_versao == TERMOS_VERSAO and a.politica_versao == POLITICA_VERSAO
        for a in aceites
    )

    return templates.TemplateResponse(
        request,
        "aceites.html",
        {
            "usuario": usuario,
            "aceites": aceites,
            "aceitou_versao_atual": aceitou_versao_atual,
            "termos_versao": TERMOS_VERSAO,
            "politica_versao": POLITICA_VERSAO,
        }
    )


@router.get("/usuarios/{user_id}/aceites.csv")
def exportar_aceites_csv(
    user_id: int,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    aceites = (
        db.query(AceiteTermos)
        .filter(AceiteTermos.user_id == user_id)
        .order_by(AceiteTermos.aceito_em.asc())
        .all()
    )

    buf = io.StringIO()
    escritor = csv.writer(buf, delimiter=";")
    escritor.writerow([
        "id", "user_id", "email", "termos_versao", "politica_versao",
        "aceito_em_brasilia", "ip", "user_agent"
    ])
    for a in aceites:
        escritor.writerow([
            a.id,
            a.user_id,
            a.email or "",
            a.termos_versao or "",
            a.politica_versao or "",
            (a.aceito_em - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if a.aceito_em else "",
            a.ip or "",
            a.user_agent or "",
        ])

    # BOM ajuda o Excel a abrir o CSV com acentos corretos.
    conteudo = "﻿" + buf.getvalue()
    nome_arquivo = f"aceites_user{user_id}.csv"
    return Response(
        content=conteudo,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )

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
    senha: str = Form(""),
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

    # Senha: para ADMIN é obrigatória (admin não passa pelo onboarding).
    # Para CLIENTE é opcional — em branco, o sistema gera uma provisória
    # aleatória e o cliente define a própria senha no link de confirmação.
    senha = (senha or "").strip()
    if eh_admin and not senha:
        return templates.TemplateResponse(
            request,
            "criar_usuario.html",
            {
                "error": "Para contas de administrador a senha é obrigatória."
            }
        )
    if not senha:
        senha = secrets.token_urlsafe(16)  # provisória; ninguém precisa conhecê-la

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

    base = os.getenv("PUBLIC_BASE_URL", _BASE_PADRAO).rstrip("/")
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

# -------------------------------------------------
# ENVIAR LINK DE NOVA SENHA (o cliente define a dele)
# -------------------------------------------------
@router.post("/usuarios/{user_id}/enviar-nova-senha")
def enviar_nova_senha_action(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(admin_session_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    # Gera um token de uso único (validade 24 horas) e monta o link.
    token = secrets.token_urlsafe(32)
    usuario.token_confirmacao = token
    usuario.token_expira = datetime.utcnow() + timedelta(hours=24)
    db.commit()

    base = os.getenv("PUBLIC_BASE_URL", _BASE_PADRAO).rstrip("/")
    link = f"{base}/nova-senha?token={token}"

    enviado = enviar_link_nova_senha(usuario.email, usuario.nome, link)

    if enviado:
        msg_email = ('<p style="font-size:15px; line-height:1.5;">O <b>e-mail com o link foi enviado '
                     f'automaticamente</b> para <b>{usuario.email}</b>.</p>')
        titulo = "Link de nova senha enviado &#9989;"
        cor_titulo = "#7aa33f"
    else:
        msg_email = ('<p style="font-size:15px; line-height:1.5;"><b>Não consegui enviar o e-mail agora.</b> '
                     'Copie o link abaixo e mande para o cliente (WhatsApp, por exemplo).</p>')
        titulo = "Link de nova senha gerado &#9888;&#65039;"
        cor_titulo = "#e0a93f"

    pagina = f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nova senha - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:560px;">
    <h1 style="color:{cor_titulo}; font-size:22px; margin-top:0;">{titulo}</h1>
    <p style="font-size:15px; line-height:1.5;">Cliente: <b>{usuario.email}</b></p>
    {msg_email}
    <p style="font-size:13px; line-height:1.5; color:#8fa97a;">O link vale por <b>24 horas</b> e só pode ser
    usado <b>uma vez</b>. Se quiser mandar pelo WhatsApp, é este:</p>
    <div style="background:#0c1826; border:1px solid #2b3a22; border-radius:8px; padding:14px; word-break:break-all; font-size:13px; color:#bcd49a;">{link}</div>
    <p style="margin-top:24px;"><a href="/admin/usuarios" style="background:#476126; color:#fff; text-decoration:none; padding:10px 18px; border-radius:8px; font-size:14px;">Voltar para a lista</a></p>
  </div>
</body></html>"""
    return HTMLResponse(pagina)

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


# ===============================================================
# ASSINATURAS (FASE 4) — PAINEL ADMIN
# ---------------------------------------------------------------
# Lista de clientes x assinatura, tela de detalhe por cliente e as
# ações: alterar plano/valor, cancelar, sincronizar com a Asaas,
# override manual (bloquear/liberar/automático) e gerar link de
# assinatura (para enviar por e-mail/WhatsApp). Tudo dentro do
# mesmo painel admin (visual dark/verde do base.html).
# ===============================================================
def _user_ou_404(db, user_id):
    u = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return u


def _voltar_detalhe(user_id, ok=None, erro=None, extra=""):
    """Monta o RedirectResponse de volta para a tela do cliente, com a
    mensagem de sucesso/erro (URL-encoded) — padrão PRG."""
    partes = []
    if ok:
        partes.append("ok=" + quote(ok))
    if erro:
        partes.append("erro=" + quote(erro))
    if extra:
        partes.append(extra)
    query = ("?" + "&".join(partes)) if partes else ""
    return RedirectResponse(f"/admin/assinaturas/{user_id}{query}", status_code=302)


@router.get("/assinaturas", response_class=HTMLResponse)
def listar_assinaturas(
    request: Request,
    busca: str = "",
    filtro: str = "",
    db: Session = Depends(get_db),
):
    if not request.session.get("admin_id"):
        return RedirectResponse("/admin/login", status_code=302)

    usuarios = db.query(Usuario).filter(Usuario.is_admin == 0).order_by(Usuario.id).all()

    # Assinatura MAIS RECENTE de cada usuário (1 query só).
    todas = (
        db.query(Assinatura)
        .order_by(Assinatura.user_id.asc(), Assinatura.id.desc())
        .all()
    )
    ass_por_user = {}
    for a in todas:
        ass_por_user.setdefault(a.user_id, a)  # 1ª por user (id desc) = a mais recente

    termo = (busca or "").strip().lower()

    # Contadores sobre o TOTAL (não sobre o filtrado).
    total = len(usuarios)
    n_ativas = n_pendentes = n_vencidas = n_sem = 0

    linhas = []
    for u in usuarios:
        a = ass_por_user.get(u.id)
        tem_sub = bool(a and a.asaas_subscription_id)
        st = a.status if a else None

        if tem_sub and st in ("active", "trial"):
            n_ativas += 1
        if st == "pending_payment":
            n_pendentes += 1
        if st in ("overdue", "suspended"):
            n_vencidas += 1
        if not tem_sub:
            n_sem += 1

        # ----- busca por nome/e-mail -----
        if termo and termo not in (u.nome or "").lower() and termo not in (u.email or "").lower():
            continue

        # ----- filtro por status -----
        if filtro == "ativas" and not (tem_sub and st in ("active", "trial")):
            continue
        if filtro == "pendentes" and st != "pending_payment":
            continue
        if filtro == "vencidas" and st not in ("overdue", "suspended"):
            continue
        if filtro == "sem_assinatura" and tem_sub:
            continue

        status_label, status_css = _status_view(a)
        controle_label, controle_css = _controle_view(a)

        linhas.append({
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "plano": (a.plano.capitalize() if a and a.plano else "—"),
            "valor": _fmt_money(a.valor) if a else "—",
            "status_label": status_label,
            "status_css": status_css,
            "controle_label": controle_label,
            "controle_css": controle_css,
            "proximo_vencimento": _fmt_data(a.proximo_vencimento) if a else "—",
            "customer_id": (a.asaas_customer_id if a and a.asaas_customer_id else "—"),
            "subscription_id": (a.asaas_subscription_id if a and a.asaas_subscription_id else "—"),
            "tem_sub": tem_sub,
            "travado": bool(a and a.valor_travado),
        })

    return templates.TemplateResponse(
        request,
        "assinaturas.html",
        {
            "linhas": linhas,
            "busca": busca or "",
            "filtro": filtro or "",
            "contadores": {
                "total": total,
                "ativas": n_ativas,
                "pendentes": n_pendentes,
                "vencidas": n_vencidas,
                "sem": n_sem,
            },
        },
    )


@router.get("/assinaturas/{user_id}", response_class=HTMLResponse)
def detalhe_assinatura(
    user_id: int,
    request: Request,
    ok: str = "",
    erro: str = "",
    gerou: str = "",
    email: str = "",
    db: Session = Depends(get_db),
):
    if not request.session.get("admin_id"):
        return RedirectResponse("/admin/login", status_code=302)

    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.assinatura_do_usuario(db, user_id)

    # Histórico de eventos desta assinatura (webhooks recebidos).
    eventos = []
    if a:
        eventos = (
            db.query(AsaasEvento)
            .filter(AsaasEvento.assinatura_id == a.id)
            .order_by(AsaasEvento.recebido_em.desc())
            .limit(50)
            .all()
        )
        for ev in eventos:
            ev.data_br = _fmt_dt_br(ev.recebido_em)

    status_label, status_css = _status_view(a)
    controle_label, controle_css = _controle_view(a)

    info = {
        "plano": (a.plano.capitalize() if a and a.plano else "—"),
        "ciclo": (_CICLO_LABEL.get(a.ciclo, a.ciclo) if a and a.ciclo else "—"),
        "valor": _fmt_money(a.valor) if a else "—",
        "status_label": status_label,
        "status_css": status_css,
        "controle_label": controle_label,
        "controle_css": controle_css,
        "controle_ate": _fmt_dt_br(a.controle_ate) if (a and a.controle_ate) else "—",
        "proximo_vencimento": _fmt_data(a.proximo_vencimento) if a else "—",
        "ultimo_pagamento": (a.ultimo_pagamento_status if a and a.ultimo_pagamento_status else "—"),
        "customer_id": (a.asaas_customer_id if a and a.asaas_customer_id else "—"),
        "subscription_id": (a.asaas_subscription_id if a and a.asaas_subscription_id else "—"),
        "last_sync": _fmt_dt_br(a.last_sync) if (a and a.last_sync) else "—",
        "criado_em": _fmt_dt_br(a.criado_em) if a else "—",
        "atualizado_em": _fmt_dt_br(a.atualizado_em) if (a and a.atualizado_em) else "—",
        "cancelado_em": _fmt_dt_br(a.cancelado_em) if (a and a.cancelado_em) else "—",
        "tem_sub": bool(a and a.asaas_subscription_id),
        "valor_travado": bool(a and a.valor_travado),
    }

    # Link de assinatura recém-gerado (para copiar / mandar no WhatsApp).
    link_gerado = None
    if gerou:
        base = os.getenv("PUBLIC_BASE_URL", _BASE_PADRAO).rstrip("/")
        link_gerado = f"{base}/confirmar?token={gerou}"

    return templates.TemplateResponse(
        request,
        "assinatura_detalhe.html",
        {
            "usuario": usuario,
            "assinatura": a,
            "info": info,
            "eventos": eventos,
            "planos": assinatura_service.planos_ativos(db),
            "ok": ok or "",
            "erro": erro or "",
            "link_gerado": link_gerado,
            "email_status": email or "",
        },
    )


@router.post("/assinaturas/{user_id}/plano")
def assinatura_alterar_plano(
    user_id: int,
    codigo: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.get_or_create_assinatura(db, usuario)
    try:
        assinatura_service.alterar_plano(db, a, codigo)
    except (ValueError, AsaasError) as e:
        return _voltar_detalhe(user_id, erro=str(e))
    return _voltar_detalhe(user_id, ok="Plano alterado com sucesso.")


@router.post("/assinaturas/{user_id}/valor")
def assinatura_alterar_valor(
    user_id: int,
    valor: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.get_or_create_assinatura(db, usuario)
    try:
        assinatura_service.alterar_valor(db, a, valor)
    except (ValueError, AsaasError) as e:
        return _voltar_detalhe(user_id, erro=str(e))
    return _voltar_detalhe(user_id, ok="Valor atualizado com sucesso.")


@router.post("/assinaturas/{user_id}/controle")
def assinatura_definir_controle(
    user_id: int,
    controle: str = Form(...),
    dias: str = Form(None),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.get_or_create_assinatura(db, usuario)
    try:
        assinatura_service.definir_controle(db, a, controle, dias)
    except ValueError as e:
        return _voltar_detalhe(user_id, erro=str(e))
    return _voltar_detalhe(user_id, ok="Controle de acesso atualizado.")


@router.post("/assinaturas/{user_id}/cancelar")
def assinatura_cancelar(
    user_id: int,
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.assinatura_do_usuario(db, user_id)
    if not a:
        return _voltar_detalhe(user_id, erro="Este cliente não tem assinatura para cancelar.")
    try:
        assinatura_service.cancelar(db, a)
    except AsaasError as e:
        return _voltar_detalhe(user_id, erro=str(e))
    return _voltar_detalhe(user_id, ok="Assinatura cancelada.")


@router.post("/assinaturas/{user_id}/sincronizar")
def assinatura_sincronizar(
    user_id: int,
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.assinatura_do_usuario(db, user_id)
    if not a or not a.asaas_subscription_id:
        return _voltar_detalhe(user_id, erro="Este cliente ainda não tem assinatura na Asaas.")
    try:
        assinatura_service.sincronizar(db, a)
    except (ValueError, AsaasError) as e:
        return _voltar_detalhe(user_id, erro=str(e))
    return _voltar_detalhe(user_id, ok="Assinatura sincronizada com a Asaas.")


@router.post("/assinaturas/{user_id}/gerar-link")
def assinatura_gerar_link(
    user_id: int,
    request: Request,
    enviar_email: str = Form(None),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)

    # Gera um token de onboarding (validade 7 dias). O cliente cai no fluxo
    # /confirmar -> /assinar -> /assinar/cartao (reaproveita o que já existe).
    token = secrets.token_urlsafe(32)
    usuario.token_confirmacao = token
    usuario.token_expira = datetime.utcnow() + timedelta(days=7)
    db.commit()

    base = os.getenv("PUBLIC_BASE_URL", _BASE_PADRAO).rstrip("/")
    link = f"{base}/confirmar?token={token}"

    email_status = "nao"
    if enviar_email:
        enviado = enviar_link_assinatura(usuario.email, usuario.nome, link)
        email_status = "ok" if enviado else "falhou"

    return _voltar_detalhe(user_id, extra=f"gerou={token}&email={email_status}")


@router.post("/assinaturas/{user_id}/desvincular")
def assinatura_desvincular(
    user_id: int,
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    n = assinatura_service.desvincular_asaas(db, user_id)
    if not n:
        return _voltar_detalhe(user_id, erro="Este cliente não tem assinatura para desvincular.")
    return _voltar_detalhe(
        user_id,
        ok="Assinatura desvinculada da Asaas. O cliente mantém o acesso e pode assinar do zero.",
    )


@router.post("/assinaturas/{user_id}/travar")
def assinatura_travar(
    user_id: int,
    travado: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    usuario = _user_ou_404(db, user_id)
    a = assinatura_service.get_or_create_assinatura(db, usuario)
    assinatura_service.definir_valor_travado(db, a, travado == "1")
    if travado == "1":
        return _voltar_detalhe(user_id, ok="Valor travado — este cliente será pulado no reajuste em massa.")
    return _voltar_detalhe(user_id, ok="Valor destravado — este cliente volta a entrar no reajuste em massa.")


# ===============================================================
# PLANOS + REAJUSTE EM MASSA (FASE 5)
# ---------------------------------------------------------------
# Edita o valor de um plano e propaga para TODAS as assinaturas
# atuais daquele plano na Asaas. Fluxo em 3 passos por segurança:
# listar -> revisar (confirmação com impacto) -> aplicar (relatório).
# ===============================================================
@router.get("/planos", response_class=HTMLResponse)
def listar_planos(
    request: Request,
    ok: str = "",
    erro: str = "",
    db: Session = Depends(get_db),
):
    if not request.session.get("admin_id"):
        return RedirectResponse("/admin/login", status_code=302)

    planos = db.query(Plano).order_by(Plano.valor.asc()).all()
    linhas = []
    for p in planos:
        alvos = assinatura_service.assinaturas_atuais_do_plano(db, p.codigo)
        reajustaveis = [a for a in alvos if not a.valor_travado]
        com_asaas = sum(1 for a in reajustaveis if a.asaas_subscription_id)
        travadas = sum(1 for a in alvos if a.valor_travado)
        linhas.append({
            "codigo": p.codigo,
            "nome": p.nome,
            "ciclo": _CICLO_LABEL.get(p.ciclo, p.ciclo),
            "valor": _fmt_money(p.valor),
            "total": len(reajustaveis),
            "com_asaas": com_asaas,
            "travadas": travadas,
        })

    return templates.TemplateResponse(
        request,
        "planos.html",
        {"linhas": linhas, "ok": ok or "", "erro": erro or ""},
    )


@router.post("/planos/{codigo}/revisar", response_class=HTMLResponse)
def revisar_reajuste(
    codigo: str,
    request: Request,
    novo_valor: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    plano = db.query(Plano).filter(Plano.codigo == codigo).first()
    if not plano:
        raise HTTPException(status_code=404, detail="Plano não encontrado")

    try:
        valor = assinatura_service.parse_valor(novo_valor)
    except ValueError as e:
        return RedirectResponse(f"/admin/planos?erro={quote(str(e))}", status_code=302)

    alvos = assinatura_service.assinaturas_atuais_do_plano(db, codigo)
    user_ids = [a.user_id for a in alvos]
    usuarios = {}
    if user_ids:
        usuarios = {u.id: u for u in db.query(Usuario).filter(Usuario.id.in_(user_ids)).all()}

    afetadas = []
    for a in alvos:
        u = usuarios.get(a.user_id)
        status_label, status_css = _status_view(a)
        afetadas.append({
            "nome": (u.nome if u else f"usuário #{a.user_id}"),
            "email": (u.email if u else ""),
            "valor_atual": _fmt_money(a.valor),
            "status_label": status_label,
            "status_css": status_css,
            "tem_sub": bool(a.asaas_subscription_id),
            "travado": bool(a.valor_travado),
        })

    return templates.TemplateResponse(
        request,
        "planos_revisar.html",
        {
            "codigo": codigo,
            "plano_nome": plano.nome,
            "ciclo": _CICLO_LABEL.get(plano.ciclo, plano.ciclo),
            "valor_antigo": _fmt_money(plano.valor),
            "valor_novo": _fmt_money(valor),
            "novo_valor_raw": novo_valor,
            "afetadas": afetadas,
            "n_reajustar": sum(1 for x in afetadas if not x["travado"]),
            "n_travadas": sum(1 for x in afetadas if x["travado"]),
            "n_com_asaas": sum(1 for x in afetadas if x["tem_sub"] and not x["travado"]),
        },
    )


@router.post("/planos/{codigo}/aplicar", response_class=HTMLResponse)
def aplicar_reajuste(
    codigo: str,
    request: Request,
    novo_valor: str = Form(...),
    admin: Usuario = Depends(admin_session_required),
    db: Session = Depends(get_db),
):
    try:
        rel = assinatura_service.reajustar_plano(db, codigo, novo_valor)
    except (ValueError, AsaasError) as e:
        return RedirectResponse(f"/admin/planos?erro={quote(str(e))}", status_code=302)

    rel["valor_antigo_fmt"] = _fmt_money(rel.get("valor_antigo"))
    rel["valor_novo_fmt"] = _fmt_money(rel.get("valor_novo"))

    return templates.TemplateResponse(
        request,
        "planos_resultado.html",
        {"rel": rel},
    )
