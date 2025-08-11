import os
import sqlalchemy as sa
from sqlalchemy import create_engine, MetaData, Table
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

# Databases
SQLITE_URL = "sqlite:///test.db"
POSTGRES_URL = os.getenv("RENDER_POSTGRES_URL")  # Your Render PostgreSQL URL

# Create engines
sqlite_engine = create_engine(SQLITE_URL)
postgres_engine = create_engine(POSTGRES_URL)

# Reflect tables
metadata = MetaData()
metadata.reflect(bind=sqlite_engine)

# Create tables in PostgreSQL (if not exists)
metadata.create_all(postgres_engine)

# Migration function
def migrate_table(table_name):
    print(f"Migrating {table_name}...")
    sqlite_table = Table(table_name, metadata, autoload_with=sqlite_engine)
    postgres_table = Table(table_name, metadata, autoload_with=postgres_engine)
    
    with sqlite_engine.connect() as src, postgres_engine.connect() as dest:
        # Fetch data
        rows = src.execute(sa.select(sqlite_table)).fetchall()
        
        if not rows:
            print(f"No data in {table_name}")
            return
        
        # Insert data
        for row in rows:
            try:
                dest.execute(
                    postgres_table.insert().values(**row._asdict())
                )
                dest.commit()
            except Exception as e:
                print(f"Error inserting row: {e}")
                dest.rollback()

if __name__ == "__main__":
    tables = ["users", "generated_tweets", "usage"]  # Add other tables as needed
    for table in tables:
        migrate_table(table)
    print("Migration complete!")
