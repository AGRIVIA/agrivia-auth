from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(
        text("ALTER TABLE usuarios ADD COLUMN vencimento_pagamento DATETIME")
    )
    conn.commit()

print("✅ Coluna vencimento_pagamento criada")
