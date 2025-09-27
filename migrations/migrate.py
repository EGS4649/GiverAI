#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    migrate_database, 
    fix_corrupted_user_data, 
    update_user_table_for_suspension,
    create_suspension_appeals_table,
    migrate_database_suspension,
    update_database_for_suspension_appeals
)

if __name__ == "__main__":
    print("Running database migrations...")
    try:
        migrate_database()
        print("✅ Basic migration complete")
        
        fix_corrupted_user_data() 
        print("✅ Data corruption fixes applied")
        
        update_user_table_for_suspension()
        print("✅ Suspension fields added")
        
        create_suspension_appeals_table()
        print("✅ Suspension appeals table created")
        
        migrate_database_suspension()
        print("✅ Suspension database migration complete")
        
        update_database_for_suspension_appeals()
        print("✅ Suspension appeals updates complete")
        
        print("🎉 All migrations completed successfully!")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        sys.exit(1)
