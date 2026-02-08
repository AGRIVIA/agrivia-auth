from database import SessionLocal
from models import Usuario

db = SessionLocal()

admin = db.query(Usuario).filter(
    Usuario.email == "admin@projetagro.com"
).first()

if not admin:
    print("❌ Usuário não encontrado")
else:
    admin.is_admin = 1
    db.commit()
    print("✅ Admin configurado com sucesso")

db.close()
