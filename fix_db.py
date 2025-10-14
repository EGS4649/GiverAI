import os
import sys
from sqlalchemy import create_engine, text

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable not set!")
    sys.exit(1)

engine = create_engine(DATABASE_URL)

def add_missing_columns():
    """Add cancellation columns to users table"""
    try:
        with engine.begin() as conn:
            # Check if columns already exist
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='users' AND column_name IN ('cancellation_date', 'cancellation_requested_at')
            """))
            
            existing_cols = {row[0] for row in result.fetchall()}
            
            # Add cancellation_date if missing
            if 'cancellation_date' not in existing_cols:
                print("Adding cancellation_date column...")
                conn.execute(text("""
                    ALTER TABLE users ADD COLUMN cancellation_date TIMESTAMP NULL
                """))
                print("✅ Added cancellation_date")
            else:
                print("⚠️  cancellation_date already exists")
            
            # Add cancellation_requested_at if missing
            if 'cancellation_requested_at' not in existing_cols:
                print("Adding cancellation_requested_at column...")
                conn.execute(text("""
                    ALTER TABLE users ADD COLUMN cancellation_requested_at TIMESTAMP NULL
                """))
                print("✅ Added cancellation_requested_at")
            else:
                print("⚠️  cancellation_requested_at already exists")
        
        print("\n🎉 Database columns fixed!")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print(f"\n📝 If columns already exist, that's fine!")
        print(f"Try restarting your app with: heroku restart -a your-app-name")
        return False

if __name__ == "__main__":
    print("🔧 Running emergency database fix...\n")
    add_missing_columns()
