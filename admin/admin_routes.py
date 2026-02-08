from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Usuario
from auth import get_current_user

# -------------------------------------------------
# Router ADMIN
# -------------------------------------------------
router = APIRouter(
    prefix="/admin",
    tags=["Admin"]
)

# -------------------------------------------------
# DEPENDÊNCIA: SOMENTE ADMIN
# -------------------------------------------------
def admin_required(user: Usuario = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Acesso negado: apenas administradores"
        )
    return user

# -------------------------------------------------
# LISTAR USUÁRIOS
# -------------------------------------------------
@router.get("/usuarios")
def listar_usuarios(
    db: Session = Depends(get_db),
    _: Usuario = Depends(admin_required)
):
    usuarios = db.query(Usuario).all()

    return [
        {
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "status": u.status,
            "is_admin": u.is_admin,
            "criado_em": u.criado_em
        }
        for u in usuarios
    ]

# -------------------------------------------------
# ALTERAR STATUS (ativar / bloquear / teste)
# -------------------------------------------------
@router.put("/usuarios/{user_id}/status")
def alterar_status(
    user_id: int,
    novo_status: str,
    db: Session = Depends(get_db),
    _: Usuario = Depends(admin_required)
):
    usuario = db.query(Usuario).filter(Usuario.id == user_id).first()

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuário não encontrado"
        )

    if novo_status not in ["ativo", "inativo", "bloqueado", "teste"]:
        raise HTTPException(
            status_code=400,
            detail="Status inválido"
        )

    usuario.status = novo_status
    db.commit()

    return {
        "success": True,
        "message": f"Status alterado para {novo_status}"
    }
