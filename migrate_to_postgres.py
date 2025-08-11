import os
import sqlalchemy as sa
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
import time

load_dotenv()

def get_engine(db_url, max_retries=3):
    for attempt in range(max_retries):
        try:
            return create_engine(db_url)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"Connection failed, retrying ({attempt + 1}/{max_retries})...")
            time.sleep(2)

def convert_sqlite_to_postgres_type(col_type):
    """Convert SQLite types to PostgreSQL compatible types"""
    if isinstance(col_type, sa.DateTime):
        return sa.TIMESTAMP()
    return col_type

def main():
    try:
        SQLITE_URL = "sqlite:///test.db"
        POSTGRES_URL = os.getenv("RENDER_POSTGRES_URL")
        
        if not POSTGRES_URL:
            raise ValueError("RENDER_POSTGRES_URL environment variable not set")

        print("Connecting to databases...")
        sqlite_engine = get_engine(SQLITE_URL)
        postgres_engine = get_engine(POSTGRES_URL)

        print("Reflecting SQLite schema...")
        sqlite_metadata = MetaData()
        sqlite_metadata.reflect(bind=sqlite_engine)

        print("Creating PostgreSQL schema...")
        postgres_metadata = MetaData()
        
        # Recreate tables with PostgreSQL-compatible types
        for table_name, table in sqlite_metadata.tables.items():
            columns = []
            for column in table.columns:
                pg_type = convert_sqlite_to_postgres_type(column.type)
                columns.append(Column(
                    column.name,
                    pg_type,
                    primary_key=column.primary_key,
                    nullable=column.nullable
                ))
            
            Table(table_name, postgres_metadata, *columns)
        
        postgres_metadata.create_all(postgres_engine)

        def migrate_table(table_name):
            print(f"\nMigrating {table_name}...")
            sqlite_table = sqlite_metadata.tables[table_name]
            postgres_table = postgres_metadata.tables[table_name]
            
            with sqlite_engine.connect() as src, postgres_engine.connect() as dest:
                rows = src.execute(sa.select(sqlite_table)).fetchall()
                
                if not rows:
                    print(f"No data in {table_name}")
                    return
                
                print(f"Found {len(rows)} rows to migrate")
                for i, row in enumerate(rows, 1):
                    try:
                        dest.execute(
                            postgres_table.insert().values(**row._asdict())
                        )
                        if i % 100 == 0:
                            print(f"Migrated {i}/{len(rows)} rows")
                        dest.commit()
                    except Exception as e:
                        print(f"Error on row {i}: {e}")
                        dest.rollback()
                        continue

        tables = ["users", "generated_tweets", "usage"]
        for table in tables:
            migrate_table(table)
            
        print("\nMigration completed successfully!")

    except Exception as e:
        print(f"\nMigration failed: {str(e)}")

if __name__ == "__main__":
    main()
