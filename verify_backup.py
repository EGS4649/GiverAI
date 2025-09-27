from main import SessionLocal, User
db = SessionLocal()
print(f"Total users: {db.query(User).count()}")
print(f"Admin users: {db.query(User).filter(User.is_admin == True).count()}")
db.close()
