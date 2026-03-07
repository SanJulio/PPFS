from database import get_db

db = get_db()
cur = db.cursor()
cur.execute("SELECT id, email, created_at FROM users")
cols = [d[0] for d in cur.description]
rows = [dict(zip(cols, row)) for row in cur.fetchall()]
cur.close()
db.close()

for r in rows:
    print(r)