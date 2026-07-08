import os
import sqlite3
import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from starlette.middleware.sessions import SessionMiddleware

from admin.admin_web import router as admin_web_router
from admin.admin_routes import router as admin_router
from sync_routes import router as sync_router

from database import SessionLocal, engine
from models import Base, Usuario, AceiteTermos, Plano, Assinatura
from auth import verify_password, create_access_token, hash_password
from termos_config import TERMOS_URL, POLITICA_URL, TERMOS_VERSAO, POLITICA_VERSAO
from planos_config import PLANOS_PADRAO
from asaas import config as asaas_config
from asaas.asaas_client import AsaasError
import assinatura_service


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
# FASE 5 — MIGRAÇÃO: COLUNA 'valor_travado' EM 'assinaturas'
# ---------------------------------------------------------------
# Adiciona a coluna que marca o valor como TRAVADO (promoção): quando
# 1, o reajuste em massa do plano PULA esse cliente. Em bancos novos a
# coluna já nasce pelo create_all; aqui é só para bancos que já tinham
# a tabela 'assinaturas' sem a coluna. Roda uma vez; idempotente.
# ===============================================================
def migrar_colunas_assinaturas():
    insp = inspect(engine)
    try:
        existentes = [c["name"] for c in insp.get_columns("assinaturas")]
    except Exception:
        return  # tabela ainda não existe; o create_all já a cria com a coluna.

    if "valor_travado" not in existentes:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE assinaturas ADD COLUMN valor_travado INTEGER DEFAULT 0"))
            print("[migracao] coluna assinaturas.valor_travado criada.")


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


def seed_planos():
    """Garante os planos padrão na 1ª vez (depois você edita os valores no painel)."""
    db = SessionLocal()
    try:
        for codigo, p in PLANOS_PADRAO.items():
            if not db.query(Plano).filter(Plano.codigo == codigo).first():
                db.add(Plano(codigo=codigo, nome=p["nome"], ciclo=p["ciclo"], valor=p["valor"], ativo=1))
        db.commit()
        print("[seed] planos garantidos.")
    except Exception as e:
        db.rollback()
        print("[seed] erro ao garantir planos:", e)
    finally:
        db.close()


# Ordem importa: cria tabela -> adiciona colunas/grandfather -> garante contas -> planos.
migrar_colunas_email()
migrar_colunas_assinaturas()
seed_inicial()
seed_planos()

# Diagnóstico seguro (NUNCA imprime a chave, só o tamanho dela).
print(f"[asaas] configurado={asaas_config.configurado()} | ambiente={asaas_config.ASAAS_ENVIRONMENT} "
      f"| base={asaas_config.ASAAS_BASE_URL} | key_len={len(asaas_config.ASAAS_API_KEY)}")

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


def _pagina_link_usado() -> str:
    """Página amigável para link já usado/inválido. Os links são de USO ÚNICO:
    depois que o cliente conclui o fluxo, clicar de novo cai aqui — e isso é
    normal, não é erro. A mensagem tranquiliza em vez de assustar."""
    return _pagina_html(
        "Este link já foi utilizado",
        "Este link já foi usado ou não é mais válido.<br><br>"
        "✅ <b>Se você acabou de concluir sua assinatura</b> (ou já ativou sua conta), "
        "está tudo certo — é só abrir o AGRIVIA e entrar normalmente.<br><br>"
        "Se você ainda não concluiu a ativação, peça um novo link ao suporte."
    )


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
            _pagina_link_usado(),
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
            _pagina_link_usado(),
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

    # Grava a PROVA do aceite e confirma o e-mail. NÃO libera o acesso ainda —
    # isso acontece após escolher o plano + cartão. MANTÉM o token p/ os próximos passos.
    user.email_verificado = 1
    db.add(AceiteTermos(
        user_id=user.id,
        email=user.email,
        termos_versao=TERMOS_VERSAO,
        politica_versao=POLITICA_VERSAO,
        ip=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    ))
    db.commit()

    # Garante uma assinatura PENDENTE (o acesso fica bloqueado até pagar).
    assinatura_service.get_or_create_assinatura(db, user)

    # Segue para a criação da senha (mantém o token na URL).
    return RedirectResponse(url=f"/definir-senha?token={token}", status_code=303)


# ===============================================================
# ASSINATURA — ESCOLHER PLANO + CARTÃO (onboarding web)
# ===============================================================
def _validar_token_onboarding(db, token):
    user = db.query(Usuario).filter(Usuario.token_confirmacao == token).first()
    if not user:
        return None
    if user.token_expira and user.token_expira < datetime.utcnow():
        return None
    return user


def _assinatura_ja_ativa(db, user):
    """True se o usuário JÁ tem assinatura ativa na Asaas. Trava anti-duplicação:
    impede o fluxo de pagamento de rodar de novo (duplo clique / recarregar a
    página) e criar uma SEGUNDA assinatura — que cobraria o cliente em dobro."""
    a = assinatura_service.assinatura_do_usuario(db, user.id)
    return bool(a and a.asaas_subscription_id and a.status == "active")


def _pagina_ja_assinou() -> str:
    return _pagina_html(
        "Assinatura já ativa 🎉",
        "Sua assinatura já está ativa e o acesso liberado — <b>não é preciso pagar de novo</b>.<br><br>"
        "É só abrir o AGRIVIA e entrar normalmente. Qualquer dúvida, fale com o suporte."
    )


def _pagina_planos(token, planos):
    opcoes = ""
    for p in planos:
        valor = f"R$ {float(p.valor or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        periodo = {"MONTHLY": "/mês", "SEMIANNUALLY": "/semestre", "YEARLY": "/ano"}.get(p.ciclo, "")
        opcoes += f'''
      <label style="display:flex; align-items:center; gap:12px; background:#0c1826; border:1px solid #2b3a22; border-radius:10px; padding:16px; margin-bottom:12px; cursor:pointer;">
        <input type="radio" name="plano" value="{p.codigo}" required style="width:18px; height:18px;">
        <span style="flex:1;"><b style="font-size:16px;">{p.nome}</b></span>
        <span style="color:#9fc35a; font-size:16px; font-weight:bold;">{valor}<span style="color:#8fa97a; font-size:13px; font-weight:normal;">{periodo}</span></span>
      </label>'''
    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Escolha o plano - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:520px; width:100%;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">Escolha seu plano</h1>
    <p style="font-size:15px; line-height:1.5;">Você informa o cartão uma vez e a renovação é automática no mesmo cartão.</p>
    <form method="post" action="/assinar">
      <input type="hidden" name="token" value="{token}">
      {opcoes}
      <button type="submit" style="margin-top:18px; width:100%; background:#476126; color:#fff; border:none; border-radius:8px; padding:13px; font-size:15px; font-weight:bold; cursor:pointer;">Continuar</button>
    </form>
  </div>
</body></html>"""


def _pagina_cartao(token, erro=None):
    erro_html = ""
    if erro:
        erro_html = ('<p style="background:rgba(192,57,43,0.15); border:1px solid #c0392b; color:#e7897d; '
                     'padding:10px 12px; border-radius:8px; font-size:14px;">' + erro + '</p>')
    inp = ("width:100%; padding:11px 12px; margin:4px 0 12px; border-radius:8px; border:1px solid #2b3a22; "
           "background:#0c1826; color:#eaf2e2; font-size:14px; box-sizing:border-box;")
    lbl = "font-size:13px; color:#8fa97a;"
    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pagamento - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:520px; width:100%;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">Dados do cartão</h1>
    <p style="font-size:13px; line-height:1.5; color:#8fa97a;">🔒 O cartão é usado só para criar a assinatura e <b>não é armazenado</b> — guardamos apenas um código (token) protegido.</p>
    {erro_html}
    <form method="post" action="/assinar/cartao"
          onsubmit="var b=this.querySelector('button[type=submit]'); b.disabled=true; b.style.opacity='0.6'; b.innerText='Processando... aguarde (não feche a página)';">
      <input type="hidden" name="token" value="{token}">
      <label style="{lbl}">Nome do titular (como está no cartão)</label>
      <input name="titular_nome" required style="{inp}">
      <label style="{lbl}">CPF do titular (só números)</label>
      <input name="cpf" required inputmode="numeric" style="{inp}">
      <label style="{lbl}">Telefone com DDD (ex: 4733334444)</label>
      <input name="telefone" required inputmode="numeric" style="{inp}">
      <div style="display:flex; gap:10px;">
        <div style="flex:2;"><label style="{lbl}">CEP</label><input name="cep" required inputmode="numeric" style="{inp}"></div>
        <div style="flex:1;"><label style="{lbl}">Nº</label><input name="numero" required style="{inp}"></div>
      </div>
      <hr style="border:none; border-top:1px solid #2b3a22; margin:6px 0 14px;">
      <label style="{lbl}">Número do cartão</label>
      <input name="numero_cartao" required inputmode="numeric" style="{inp}">
      <div style="display:flex; gap:10px;">
        <div style="flex:1;"><label style="{lbl}">Mês (MM)</label><input name="validade_mes" required maxlength="2" placeholder="12" style="{inp}"></div>
        <div style="flex:1;"><label style="{lbl}">Ano (AAAA)</label><input name="validade_ano" required maxlength="4" placeholder="2028" style="{inp}"></div>
        <div style="flex:1;"><label style="{lbl}">CVV</label><input name="cvv" required maxlength="4" style="{inp}"></div>
      </div>
      <button type="submit" style="margin-top:14px; width:100%; background:#476126; color:#fff; border:none; border-radius:8px; padding:13px; font-size:15px; font-weight:bold; cursor:pointer;">Assinar e liberar acesso</button>
    </form>
  </div>
</body></html>"""


@app.get("/assinar", response_class=HTMLResponse)
def assinar_get(token: str, db: Session = Depends(get_db)):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    if _assinatura_ja_ativa(db, user):
        return HTMLResponse(_pagina_ja_assinou())
    return HTMLResponse(_pagina_planos(token, assinatura_service.planos_ativos(db)))


@app.post("/assinar", response_class=HTMLResponse)
def assinar_post(token: str = Form(...), plano: str = Form(...), db: Session = Depends(get_db)):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    if _assinatura_ja_ativa(db, user):
        return HTMLResponse(_pagina_ja_assinou())
    try:
        assinatura_service.definir_plano(db, user, plano)
    except ValueError:
        return HTMLResponse(_pagina_planos(token, assinatura_service.planos_ativos(db)), status_code=400)
    return RedirectResponse(url=f"/assinar/cartao?token={token}", status_code=303)


@app.get("/assinar/cartao", response_class=HTMLResponse)
def cartao_get(token: str, db: Session = Depends(get_db)):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    if _assinatura_ja_ativa(db, user):
        return HTMLResponse(_pagina_ja_assinou())
    return HTMLResponse(_pagina_cartao(token))


@app.post("/assinar/cartao", response_class=HTMLResponse)
def cartao_post(
    request: Request,
    token: str = Form(...),
    titular_nome: str = Form(...),
    cpf: str = Form(...),
    telefone: str = Form(...),
    cep: str = Form(...),
    numero: str = Form(...),
    numero_cartao: str = Form(...),
    validade_mes: str = Form(...),
    validade_ano: str = Form(...),
    cvv: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    if _assinatura_ja_ativa(db, user):
        # Já assinou (ex.: clicou de novo depois de concluir) -> NÃO cobra de novo.
        return HTMLResponse(_pagina_ja_assinou())

    # Cartão SÓ em memória — passado pro serviço, tokenizado e descartado.
    cartao = {
        "holderName": titular_nome.strip(),
        "number": numero_cartao.replace(" ", "").strip(),
        "expiryMonth": validade_mes.strip(),
        "expiryYear": validade_ano.strip(),
        "ccv": cvv.strip(),
    }
    titular = {
        "name": titular_nome.strip(),
        "email": user.email,
        "cpfCnpj": "".join(c for c in cpf if c.isdigit()),
        "postalCode": "".join(c for c in cep if c.isdigit()),
        "addressNumber": numero.strip(),
        "phone": "".join(c for c in telefone if c.isdigit()),
    }

    try:
        assinatura_service.criar_assinatura_completa(db, user, cartao, titular, _client_ip(request))
    except (AsaasError, ValueError) as e:
        return HTMLResponse(_pagina_cartao(token, erro=str(e)), status_code=400)
    except Exception:
        return HTMLResponse(_pagina_cartao(token, erro="Não foi possível processar o pagamento agora. Tente novamente."), status_code=500)

    return HTMLResponse(
        _pagina_html("Assinatura ativa! 🎉", "Pagamento aprovado e assinatura criada. Sua conta está liberada — já pode entrar no AGRIVIA.")
    )


# ===============================================================
# SENHA DEFINIDA PELO PRÓPRIO USUÁRIO
# ---------------------------------------------------------------
# Dois fluxos, mesmo formulário:
#  1) /definir-senha  -> passo do ONBOARDING (após aceitar os termos,
#     antes de escolher o plano). MANTÉM o token vivo p/ continuar.
#  2) /nova-senha     -> link avulso "esqueci a senha" gerado pelo
#     painel admin. Ao concluir, LIMPA o token (link de uso único).
# A senha nunca aparece em log; só o hash (bcrypt) vai pro banco.
# ===============================================================
_SENHA_MIN = 8


def _pagina_senha(action, token, titulo, subtitulo, botao, erro=None):
    erro_html = ""
    if erro:
        erro_html = ('<p style="background:rgba(192,57,43,0.15); border:1px solid #c0392b; color:#e7897d; '
                     'padding:10px 12px; border-radius:8px; font-size:14px;">' + erro + '</p>')
    inp = ("width:100%; padding:11px 12px; margin:4px 0 12px; border-radius:8px; border:1px solid #2b3a22; "
           "background:#0c1826; color:#eaf2e2; font-size:14px; box-sizing:border-box;")
    lbl = "font-size:13px; color:#8fa97a;"
    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} - AGRIVIA</title></head>
<body style="font-family: Arial, sans-serif; background:#0c1826; color:#eaf2e2; margin:0; display:flex; min-height:100vh; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#0b1320; border:1px solid #476126; border-radius:14px; padding:36px; max-width:520px; width:100%;">
    <h1 style="color:#7aa33f; font-size:22px; margin-top:0;">{titulo}</h1>
    <p style="font-size:15px; line-height:1.5;">{subtitulo}</p>
    {erro_html}
    <form method="post" action="{action}">
      <input type="hidden" name="token" value="{token}">
      <label style="{lbl}">Nova senha (mínimo {_SENHA_MIN} caracteres)</label>
      <input name="senha" type="password" required minlength="{_SENHA_MIN}" style="{inp}">
      <label style="{lbl}">Repita a nova senha</label>
      <input name="senha2" type="password" required minlength="{_SENHA_MIN}" style="{inp}">
      <button type="submit" style="margin-top:14px; width:100%; background:#476126; color:#fff; border:none; border-radius:8px; padding:13px; font-size:15px; font-weight:bold; cursor:pointer;">{botao}</button>
    </form>
  </div>
</body></html>"""


def _validar_senhas(senha, senha2):
    """Devolve a mensagem de erro (ou None se estiver tudo certo)."""
    senha = (senha or "").strip()
    senha2 = (senha2 or "").strip()
    if len(senha) < _SENHA_MIN:
        return f"A senha precisa ter pelo menos {_SENHA_MIN} caracteres."
    if senha != senha2:
        return "As duas senhas não são iguais. Digite a mesma senha nos dois campos."
    return None


# ---------- 1) Passo do ONBOARDING ----------
@app.get("/definir-senha", response_class=HTMLResponse)
def definir_senha_get(token: str, db: Session = Depends(get_db)):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    return HTMLResponse(_pagina_senha(
        "/definir-senha", token,
        "Crie sua senha",
        f"Conta: <b>{user.email}</b><br>Esta será a senha para entrar no AGRIVIA. "
        "Se você já usa o sistema e quer manter a senha atual, basta digitá-la de novo.",
        "Salvar senha e continuar",
    ))


@app.post("/definir-senha", response_class=HTMLResponse)
def definir_senha_post(
    token: str = Form(...),
    senha: str = Form(...),
    senha2: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)

    erro = _validar_senhas(senha, senha2)
    if erro:
        return HTMLResponse(_pagina_senha(
            "/definir-senha", token,
            "Crie sua senha",
            f"Conta: <b>{user.email}</b><br>Esta será a senha para entrar no AGRIVIA. "
            "Se você já usa o sistema e quer manter a senha atual, basta digitá-la de novo.",
            "Salvar senha e continuar",
            erro=erro,
        ), status_code=400)

    user.senha_hash = hash_password(senha.strip())
    user.atualizado_em = datetime.utcnow()
    db.commit()

    # Segue para a escolha do plano (MANTÉM o token vivo).
    return RedirectResponse(url=f"/assinar?token={token}", status_code=303)


# ---------- 2) Link avulso "ESQUECI A SENHA" (gerado pelo admin) ----------
@app.get("/nova-senha", response_class=HTMLResponse)
def nova_senha_get(token: str, db: Session = Depends(get_db)):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)
    return HTMLResponse(_pagina_senha(
        "/nova-senha", token,
        "Criar nova senha",
        f"Conta: <b>{user.email}</b><br>Defina abaixo a sua nova senha de acesso ao AGRIVIA.",
        "Salvar nova senha",
    ))


@app.post("/nova-senha", response_class=HTMLResponse)
def nova_senha_post(
    token: str = Form(...),
    senha: str = Form(...),
    senha2: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _validar_token_onboarding(db, token)
    if not user:
        return HTMLResponse(_pagina_link_usado(), status_code=400)

    erro = _validar_senhas(senha, senha2)
    if erro:
        return HTMLResponse(_pagina_senha(
            "/nova-senha", token,
            "Criar nova senha",
            f"Conta: <b>{user.email}</b><br>Defina abaixo a sua nova senha de acesso ao AGRIVIA.",
            "Salvar nova senha",
            erro=erro,
        ), status_code=400)

    user.senha_hash = hash_password(senha.strip())
    # Link de uso ÚNICO: depois de trocar a senha, o token morre.
    user.token_confirmacao = None
    user.token_expira = None
    user.atualizado_em = datetime.utcnow()
    db.commit()

    return HTMLResponse(_pagina_html(
        "Senha alterada! ✅",
        "Sua nova senha foi salva. Já pode entrar no AGRIVIA com ela."
    ))


# ===============================================================
# WEBHOOK ASAAS (mantém o status da assinatura sozinho)
# ===============================================================
@app.post("/webhooks/asaas")
async def webhook_asaas(request: Request, db: Session = Depends(get_db)):
    # Valida o token secreto que VOCÊ configura no painel da Asaas.
    if asaas_config.ASAAS_WEBHOOK_TOKEN:
        if request.headers.get("asaas-access-token", "") != asaas_config.ASAAS_WEBHOOK_TOKEN:
            raise HTTPException(status_code=401, detail="webhook nao autorizado")
    try:
        payload = await request.json()
        assinatura_service.processar_webhook(db, payload)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print("[asaas] erro no webhook:", e)
        raise HTTPException(status_code=500, detail="erro ao processar webhook")
    return {"received": True}


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

    # ACESSO por assinatura (+ override manual do admin).
    liberado, motivo = assinatura_service.acesso_liberado(db, user)
    if not liberado:
        _MSG = {
            "bloqueado_admin": "Conta bloqueada. Fale com o suporte.",
            "bloqueado_manual": "Acesso bloqueado pelo administrador. Fale com o suporte.",
            "overdue": "Sua assinatura está vencida. Regularize o pagamento para continuar.",
            "suspended": "Sua assinatura está suspensa. Fale com o suporte.",
            "cancelled": "Sua assinatura foi cancelada. Fale com o suporte para reativar.",
            "pending_payment": "Pagamento pendente. Conclua sua assinatura para liberar o acesso.",
            "sem_assinatura": "Conta inativa. Fale com o suporte.",
        }
        raise HTTPException(status_code=403, detail=_MSG.get(motivo, "Acesso indisponível. Fale com o suporte."))

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
