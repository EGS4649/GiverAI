#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app, Base  # Your Flask app & SQLAlchemy Base
from models import *  # Import ALL models (including ScheduledEmails)

if __name__ == "__main__":
    print("Creating scheduled emails table...")
    try:
        with app.app_context():
            engine = app.extensions['migrate'].db.engine  # or however you get engine
            Base.metadata.create_all(bind=engine, checkfirst=True)
            print("✅ Scheduled emails table created!")
    except Exception as e:
        print(f"❌ Failed to create table: {e}")
        sys.exit(1)
