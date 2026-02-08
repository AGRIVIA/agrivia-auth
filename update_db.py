from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text(
        "ALTER TABLE usuarios ADD COLUMN is_admin INTEGER DEFAULT 0"
    ))
    conn.commit()

print("Coluna is_admin criada com sucesso")
