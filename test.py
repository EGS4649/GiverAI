from models import User
from database import SessionLocal

db = SessionLocal()
user = db.query(User).filter(User.email == "irtan16@mail.ru").first()

print(f"Last login: {user.last_login}")
print(f"Created: {user.created_at}")
print(f"Email verified: {user.email_verified}")
