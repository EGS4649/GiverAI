#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import migrate_database, fix_corrupted_user_data, update_user_table_for_suspension

if __name__ == "__main__":
    print("Running database migrations...")
    migrate_database()
    fix_corrupted_user_data()  
    update_user_table_for_suspension()
    print("Migrations complete!")
