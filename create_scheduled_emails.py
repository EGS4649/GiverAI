#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app, Base, ScheduledEmail  # Import directly from main

if __name__ == "__main__":
    print("Creating scheduled emails table...")
    try:
        with app.app_context():
            # This creates ALL tables in Base.metadata (including your new one)
            Base.metadata.create_all(bind=app.extensions['migrate'].db.engine, checkfirst=True)
            print("✅ Scheduled emails table created!")
    except Exception as e:
        print(f"❌ Failed to create table: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
