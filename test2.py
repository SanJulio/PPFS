import os
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("DATABASE_URL")
print(f"DATABASE_URL = {url}")