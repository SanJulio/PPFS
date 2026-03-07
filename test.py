print("step 1 - importing database")
from database import init_db, get_db, USE_POSTGRES
print(f"step 2 - USE_POSTGRES = {USE_POSTGRES}")

print("step 3 - running init_db")
init_db()
print("step 4 - init_db complete")

print("step 5 - importing app")
from app import app
print("step 6 - app imported successfully")