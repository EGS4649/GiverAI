#!/usr/bin/env python3
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

def make_admin(email):
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE users SET is_admin = TRUE WHERE email = :email"),
            {"email": email}
        )
        if result.rowcount > 0:
            print(f"✅ {email} is now an admin")
        else:
            print(f"❌ User {email} not found")

if __name__ == "__main__":
    make_admin("support@giverai.me") 
