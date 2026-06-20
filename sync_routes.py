# ===============================================================
# FASE 2 — SINCRONIZAÇÃO DO BANCO NA NUVEM (modelo "cópia inteira")
# ---------------------------------------------------------------
# 3 rotas, todas protegidas pelo login (token JWT do app):
#   GET  /api/sync/status    -> diz qual a versão atual na nuvem
#   POST /api/sync/upload    -> recebe a cópia do banco (gzip) e sobe
#   GET  /api/sync/download  -> devolve a última cópia da nuvem
#
# TRAVA DE VERSÃO: o upload manda 'base_version' (a versão que o
# cliente tinha como base). O servidor só aceita se base_version
# for igual à versão atual dele. Se for diferente, devolve 409
# (conflito) — sinal de que a nuvem tem dado mais novo e o cliente
# precisa baixar antes de subir. Assim um PC antigo nunca
# sobrescreve dados mais recentes.
# ===============================================================
from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import DbSnapshot, Usuario
from auth import get_current_user

router = APIRouter(prefix="/api/sync", tags=["Sync"])

# Quantas cópias antigas manter por usuário (para poder "voltar atrás").
MAX_HISTORICO = 5


def _versao_atual(db: Session, user_id: int) -> int:
    """Maior número de versão que esse usuário já tem na nuvem (0 se nenhuma)."""
    v = (
        db.query(func.max(DbSnapshot.version))
        .filter(DbSnapshot.user_id == user_id)
        .scalar()
    )
    return int(v or 0)


# ---------------------------------------------------------------
# STATUS — qual a versão atual na nuvem
# ---------------------------------------------------------------
@router.get("/status")
def status_sync(
    db: Session = Depends(get_db),
    user: Usuario = Depends(get_current_user),
):
    snap = (
        db.query(DbSnapshot)
        .filter(DbSnapshot.user_id == user.id)
        .order_by(DbSnapshot.version.desc())
        .first()
    )
    if not snap:
        return {
            "current_version": 0,
            "last_at": None,
            "tamanho_bytes": 0,
            "device_id": None,
        }
    return {
        "current_version": snap.version,
        "last_at": snap.criado_em.isoformat() if snap.criado_em else None,
        "tamanho_bytes": snap.tamanho_bytes or 0,
        "device_id": snap.device_id,
    }


# ---------------------------------------------------------------
# UPLOAD — sobe a cópia do banco (com trava de versão)
# ---------------------------------------------------------------
@router.post("/upload")
def upload_snapshot(
    base_version: int = Form(...),
    device_id: str = Form(None),
    arquivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: Usuario = Depends(get_current_user),
):
    atual = _versao_atual(db, user.id)

    # TRAVA DE VERSÃO: só aceita se o cliente está baseado na versão atual.
    if base_version != atual:
        raise HTTPException(
            status_code=409,
            detail={
                "erro": "conflito_versao",
                "servidor": atual,
                "cliente_base": base_version,
                "mensagem": "A nuvem tem uma versão diferente. Baixe antes de subir.",
            },
        )

    conteudo = arquivo.file.read()
    if not conteudo:
        raise HTTPException(status_code=400, detail="arquivo_vazio")

    nova_versao = atual + 1
    snap = DbSnapshot(
        user_id=user.id,
        version=nova_versao,
        conteudo=conteudo,
        tamanho_bytes=len(conteudo),
        device_id=device_id,
    )
    db.add(snap)
    db.commit()

    # Mantém apenas as últimas MAX_HISTORICO cópias deste usuário.
    antigas = (
        db.query(DbSnapshot)
        .filter(DbSnapshot.user_id == user.id)
        .order_by(DbSnapshot.version.desc())
        .offset(MAX_HISTORICO)
        .all()
    )
    if antigas:
        for a in antigas:
            db.delete(a)
        db.commit()

    print(f"[sync] upload user={user.id} versao={nova_versao} tamanho={len(conteudo)} bytes")
    return {"success": True, "version": nova_versao}


# ---------------------------------------------------------------
# DOWNLOAD — baixa a última cópia da nuvem
# ---------------------------------------------------------------
@router.get("/download")
def download_snapshot(
    db: Session = Depends(get_db),
    user: Usuario = Depends(get_current_user),
):
    snap = (
        db.query(DbSnapshot)
        .filter(DbSnapshot.user_id == user.id)
        .order_by(DbSnapshot.version.desc())
        .first()
    )
    if not snap:
        raise HTTPException(status_code=404, detail="sem_copia")

    print(f"[sync] download user={user.id} versao={snap.version}")
    return Response(
        content=snap.conteudo,
        media_type="application/octet-stream",
        headers={
            "X-DB-Version": str(snap.version),
            "Content-Disposition": "attachment; filename=agrivia_nuvem.db.gz",
        },
    )
