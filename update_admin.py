import sqlite3

conn = sqlite3.connect("usuarios.db")
cur = conn.cursor()

# adiciona coluna is_admin (se não existir)
try:
    cur.execute("ALTER TABLE usuarios ADD COLUMN is_admin INTEGER DEFAULT 0")
    print("Coluna is_admin criada")
except Exception as e:
    print("Coluna já existe ou erro:", e)

# torna o admin administrador
cur.execute("""
    UPDATE usuarios
    SET is_admin = 1
    WHERE email = 'admin@projetagro.com'
""")

conn.commit()

# conferir
cur.execute("SELECT id, email, status, is_admin FROM usuarios")
rows = cur.fetchall()

print("\nUsuários:")
for r in rows:
    print(r)

conn.close()
