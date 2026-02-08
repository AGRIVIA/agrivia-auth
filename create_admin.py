from database import SessionLocal
from models import Usuario
from auth import hash_password


def create_admin():
    db = SessionLocal()

    email = "admin@projetagro.com"
    nome = "Administrador"
    senha = "admin123"
    status = "ativo"

    exists = db.query(Usuario).filter(Usuario.email == email).first()
    if exists:
        print("❌ Admin já existe.")
        return

    user = Usuario(
        nome=nome,
        email=email,
        senha_hash=hash_password(senha),
        status=status
    )

    db.add(user)
    db.commit()
    db.close()

    print("✅ ADMIN criado com sucesso!")
    print("📧 Email:", email)
    print("🔑 Senha:", senha)


if __name__ == "__main__":
    create_admin()
