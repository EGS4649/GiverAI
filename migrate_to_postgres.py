import os
import sqlalchemy as sa
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Boolean, TIMESTAMP
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
import time
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

def get_engine(db_url, max_retries=3):
    for attempt in range(max_retries):
        try:
            return create_engine(db_url)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            logger.info(f"Connection failed, retrying ({attempt + 1}/{max_retries})...")
            time.sleep(2)

def convert_sqlite_to_postgres_type(col_type):
    """Convert SQLite types to PostgreSQL compatible types"""
    if isinstance(col_type, sa.DateTime):
        return TIMESTAMP()
    return col_type

def main():
    try:
        SQLITE_URL = "sqlite:///test.db"
        POSTGRES_URL = os.getenv("RENDER_POSTGRES_URL")
        
        if not POSTGRES_URL:
            raise ValueError("RENDER_POSTGRES_URL environment variable not set")

        logger.info("Connecting to databases...")
        sqlite_engine = get_engine(SQLITE_URL)
        postgres_engine = get_engine(POSTGRES_URL)

        logger.info("Reflecting SQLite schema...")
        sqlite_metadata = MetaData()
        sqlite_metadata.reflect(bind=sqlite_engine)
        sqlite_tables = set(sqlite_metadata.tables.keys())
        logger.info(f"Tables in SQLite: {sqlite_tables}")

        logger.info("Creating PostgreSQL schema (if not exists)...")
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
        
        # Create tables only if they don't exist
        postgres_metadata.create_all(postgres_engine, checkfirst=True)

        def migrate_table(table_name):
            logger.info(f"\nMigrating {table_name}...")
            
            # Check if table exists in SQLite
            if table_name not in sqlite_metadata.tables:
                logger.warning(f"Table {table_name} not found in SQLite database! Skipping...")
                return
                
            sqlite_table = sqlite_metadata.tables[table_name]
            postgres_table = postgres_metadata.tables[table_name]
            
            with sqlite_engine.begin() as src, postgres_engine.begin() as dest:
                # Check if table is empty in PostgreSQL
                try:
                    existing_count = dest.execute(sa.select(sa.func.count()).select_from(postgres_table)).scalar()
                except Exception as e:
                    logger.error(f"Error checking existing data: {e}")
                    logger.info("Creating missing table in PostgreSQL...")
                    postgres_table.create(dest)
                    existing_count = 0
                
                if existing_count > 0:
                    logger.info(f"Skipping {table_name} - already has {existing_count} rows")
                    return
                
                try:
                    rows = src.execute(sa.select(sqlite_table)).fetchall()
                except Exception as e:
                    logger.error(f"Error reading from SQLite: {e}")
                    return
                
                if not rows:
                    logger.info(f"No data in {table_name}")
                    return
                
                logger.info(f"Found {len(rows)} rows to migrate")
                for i, row in enumerate(rows, 1):
                    try:
                        # For users table, let PostgreSQL generate new IDs
                        if table_name == "users":
                            row_dict = row._asdict()
                            del row_dict['id']  # Let PostgreSQL generate the ID
                            dest.execute(
                                postgres_table.insert().values(**row_dict)
                            )
                        else:
                            dest.execute(
                                postgres_table.insert().values(**row._asdict())
                            )
                        
                        if i % 100 == 0 or i == len(rows):
                            logger.info(f"Migrated {i}/{len(rows)} rows")
                    except Exception as e:
                        logger.error(f"Error on row {i}: {e}")
                        # Continue with next row instead of failing completely
                        continue

        tables = ["users", "generated_tweets", "usage"]
        for table in tables:
            migrate_table(table)
            
        logger.info("\nMigration completed successfully!")

    except Exception as e:
        logger.error(f"\nMigration failed: {str(e)}")
        logger.exception("Migration error details:")

if __name__ == "__main__":
    main()
