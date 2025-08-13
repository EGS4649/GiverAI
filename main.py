import datetime
import os
import secrets
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy import LargeBinary
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, ProgrammingError 
from sqlalchemy.orm import defer
from openai import OpenAI
from pydantic import BaseModel
from jose import JWTError, jwt
import stripe
import json
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_CREATOR_PRICE_ID = os.getenv("STRIPE_CREATOR_PRICE_ID")
STRIPE_SMALL_TEAM_PRICE_ID = os.getenv("STRIPE_SMALL_TEAM_PRICE_ID")
STRIPE_AGENCY_PRICE_ID = os.getenv("STRIPE_AGENCY_PRICE_ID")
STRIPE_ENTERPRISE_PRICE_ID = os.getenv("STRIPE_ENTERPRISE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

try:
    import bcrypt
    print("bcrypt module imported successfully")
except Exception as e:
    print("bcrypt import error:", e)

# ----- DB Setup -----
DATABASE_URL = os.getenv("DATABASE_URL")  # Should be your Render PostgreSQL URL
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ----- User & Usage Models -----
class GeneratedTweet(Base):
    __tablename__ = "generated_tweets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    tweet_text = Column(String)
    generated_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User")
    
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(LargeBinary, nullable=False)
    plan = Column(String, default="free")  # Now supports: free, creator, small_team, agency, enterprise
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    stripe_customer_id = Column(String, nullable=True)  
    api_key = Column(String, nullable=True)  # Added API key column
    is_admin = Column(Boolean, default=False)
    role = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    goals = Column(String, nullable=True)
    posting_frequency = Column(String, nullable=True)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    date = Column(String)
    count = Column(Integer, default=0)
    user = relationship("User")

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    email = Column(String, index=True)
    role = Column(String, default="editor")
    status = Column(String, default="pending")  # pending, active, removed
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User")

# Account Management Models
class PasswordChange(BaseModel):
    current_password: str
    new_password: str

class EmailChange(BaseModel):
    new_email: str

# ----- Auth Setup -----
SECRET_KEY = "your-secret-key-here"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

# Also update the hash_password function with better debugging
def hash_password(password: str) -> bytes:
    """Hash a password and return bytes for database storage"""
    try:
        print(f"hash_password called with password type: {type(password)}")
        
        # Ensure we're working with bytes for bcrypt
        if isinstance(password, str):
            password_bytes = password.encode('utf-8')
            print("Converted password string to bytes")
        else:
            password_bytes = password
            print("Password was already bytes")
        
        print("Generating salt...")
        salt = bcrypt.gensalt()
        print(f"Salt type: {type(salt)}")
        
        print("Hashing password...")
        hashed = bcrypt.hashpw(password_bytes, salt)
        print(f"bcrypt.hashpw returned type: {type(hashed)}")
        
        # bcrypt should always return bytes, but let's be extra safe
        if isinstance(hashed, str):
            print("WARNING: bcrypt returned string, converting to bytes")
            return hashed.encode('utf-8')
        
        if not isinstance(hashed, bytes):
            raise ValueError(f"bcrypt returned unexpected type: {type(hashed)}")
        
        print(f"Returning hashed password of type: {type(hashed)}")
        return hashed
        
    except Exception as e:
        print(f"Password hashing error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        raise ValueError(f"Failed to hash password: {str(e)}")

def verify_password(plain_password: str, hashed_password: bytes) -> bool:
    """Verify a password against a hash"""
    try:
        print(f"verify_password called with hash type: {type(hashed_password)}")
        
        # Ensure plain_password is bytes
        if isinstance(plain_password, str):
            plain_password_bytes = plain_password.encode('utf-8')
        else:
            plain_password_bytes = plain_password
        
        # Handle case where hashed_password might be stored as string
        if isinstance(hashed_password, str):
            print("WARNING: hashed_password is string, converting to bytes")
            hashed_password = hashed_password.encode('utf-8')
        
        return bcrypt.checkpw(plain_password_bytes, hashed_password)
        
    except Exception as e:
        print(f"Password verification error: {str(e)}")
        return False

def verify_password(plain_password: str, hashed_password: bytes) -> bool:
    # Ensure plain_password is encoded to bytes
    plain_password_bytes = plain_password.encode('utf-8')
    return bcrypt.checkpw(plain_password_bytes, hashed_password)

def create_access_token(data: dict, expires_delta=None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.utcnow() + expires_delta
    else:
        expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=15)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user(db, username: str):
    try:
        # Defer loading password in non-auth routes
        return db.query(User).options(defer(User.hashed_password)).filter(User.username == username).first()
    except ProgrammingError as e:
        if "column users.is_admin does not exist" in str(e):
            migrate_database()
            return db.query(User).options(defer(User.hashed_password)).filter(User.username == username).first()
        raise

def authenticate_user(db, username: str, password: str):
    user = get_user(db, username)
    if not user or not verify_password(password, user.hashed_password): 
        return None
    return user

def get_plan_features(plan_name):
    features = {
        "free": {
            "daily_limit": 15,
            "history_days": 1,
            "customization": "basic",
            "export": False,
            "scheduling": False,
            "analytics": False,
            "team_seats": 0,
            "support": "standard",
            "white_label": False,
            "api_access": False
        },
        "creator": {
            "daily_limit": float('inf'),
            "history_days": 60,
            "customization": "advanced",
            "export": True,
            "scheduling": "limited",
            "analytics": False,
            "team_seats": 1,
            "support": "priority_email",
            "white_label": False,
            "api_access": False
        },
        "small_team": {
            "daily_limit": float('inf'),
            "history_days": 60,
            "customization": "advanced_team",
            "export": True,
            "scheduling": "standard",
            "analytics": False,
            "team_seats": 5,
            "support": "priority",
            "white_label": False,
            "api_access": False
        },
        "agency": {
            "daily_limit": float('inf'),
            "history_days": 90,
            "customization": "advanced_analytics",
            "export": True,
            "scheduling": "advanced",
            "analytics": True,
            "team_seats": 15,
            "support": "dedicated",
            "white_label": True,
            "api_access": False
        },
        "enterprise": {
            "daily_limit": float('inf'),
            "history_days": 365,
            "customization": "fully_custom",
            "export": True,
            "scheduling": "unlimited",
            "analytics": True,
            "team_seats": float('inf'),
            "support": "24/7_dedicated",
            "white_label": True,
            "api_access": True
        }
    }
    return features.get(plan_name, features["free"])

def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    
    if not token:
        raise credentials_exception
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            raise credentials_exception
        
        # Attach features to the user object
        user.features = get_plan_features(user.plan)
        return user
    finally:
        db.close()

def get_optional_user(request: Request):
    try:
        user = get_current_user(request)
        # Ensure features exist even for optional users
        if not hasattr(user, 'features'):
            user.features = get_plan_features(user.plan)
        return user
    except Exception:
        return None
        
def apply_plan_features(user):
    if not hasattr(user, 'features'):
        user.features = get_plan_features(user.plan)
    return user
    
# ---- FastAPI Setup -----
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY", "your-openrouter-api-key-here")
)

# Add this to your migrate_database function
def migrate_database():
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
    # Check and add all missing columns
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        
        # Check if hashed_password column has the right type
        user_columns = inspector.get_columns('users')
        hashed_password_col = next((col for col in user_columns if col['name'] == 'hashed_password'), None)
        
        if hashed_password_col:
            print(f"hashed_password column type: {hashed_password_col['type']}")
            # If it's not bytea (PostgreSQL's binary type), we need to fix it
            if 'bytea' not in str(hashed_password_col['type']).lower():
                print("WARNING: hashed_password column is not bytea type")
                # You might need to recreate this column
                try:
                    with engine.begin() as conn:
                        # First, let's try to alter the column type
                        conn.execute(text("ALTER TABLE users ALTER COLUMN hashed_password TYPE bytea USING hashed_password::bytea"))
                        print("Successfully converted hashed_password column to bytea")
                except Exception as e:
                    print(f"Could not alter column type: {e}")
                    print("You may need to manually fix the database schema")
        
        # List of all columns that should exist
        required_columns = {
            'is_admin': "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE",
            'api_key': "ALTER TABLE users ADD COLUMN api_key VARCHAR",
            'stripe_customer_id': "ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR",
            'plan': "ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'",
            'role': "ALTER TABLE users ADD COLUMN role VARCHAR",
            'industry': "ALTER TABLE users ADD COLUMN industry VARCHAR",
            'goals': "ALTER TABLE users ADD COLUMN goals VARCHAR",
            'posting_frequency': "ALTER TABLE users ADD COLUMN posting_frequency VARCHAR"
        }
        
        for col_name, sql in required_columns.items():
            if col_name not in columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    print(f"Added {col_name} column to users table")
                except Exception as e:
                    print(f"Error adding {col_name}: {e}")

    # First, let's create a function to check and fix the database
def fix_corrupted_user_data():
    """Fix corrupted hashed_password data in the database"""
    from sqlalchemy import create_engine, text
    
    engine = create_engine(DATABASE_URL)
    
    try:
        with engine.begin() as conn:
            # First, let's see what data we have
            print("=== Checking user data ===")
            
            # Get all users without trying to process hashed_password through SQLAlchemy
            result = conn.execute(text("""
                SELECT id, username, email, hashed_password, 
                       pg_typeof(hashed_password) as password_type
                FROM users
            """))
            
            rows = result.fetchall()
            print(f"Found {len(rows)} users in database")
            
            for row in rows:
                print(f"User ID: {row[0]}, Username: {row[1]}")
                print(f"Password type in DB: {row[4]}")
                print(f"Password data type in Python: {type(row[3])}")
                
                # If the password is stored as text instead of bytea, we need to fix it
                if row[4] == 'text' or isinstance(row[3], str):
                    print(f"WARNING: User {row[1]} has corrupted password data")
                    
                    # For now, let's set a temporary password that they can change
                    temp_password = "TempPassword123!"
                    temp_hash = hash_password(temp_password)
                    
                    conn.execute(text("""
                        UPDATE users 
                        SET hashed_password = :new_hash 
                        WHERE id = :user_id
                    """), {
                        "new_hash": temp_hash,
                        "user_id": row[0]
                    })
                    
                    print(f"Fixed password for user {row[1]} - temp password: {temp_password}")
            
            print("=== User data check completed ===")
            
    except Exception as e:
        print(f"Error checking user data: {e}")
        import traceback
        traceback.print_exc()

# Modified registration function that avoids the corrupted data
@app.post("/register", response_class=HTMLResponse)  
def register_user(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        print(f"Starting registration for username: {username}")
        
        # Use raw SQL to avoid SQLAlchemy processing corrupted data
        from sqlalchemy import text
        
        # Check username exists using raw SQL
        result = db.execute(text("""
            SELECT COUNT(*) FROM users WHERE username = :username
        """), {"username": username})
        username_count = result.scalar()
        
        if username_count > 0:
            print(f"Username {username} already exists")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Username already exists"
            })
        
        # Check email exists using raw SQL  
        result = db.execute(text("""
            SELECT COUNT(*) FROM users WHERE email = :email
        """), {"email": email})
        email_count = result.scalar()
        
        if email_count > 0:
            print(f"Email {email} already registered")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Email already registered"
            })
        
        # Hash the password
        print("Hashing password...")
        hashed_password = hash_password(password)
        print(f"Password hashed successfully, type: {type(hashed_password)}")
        
        # Insert using raw SQL to avoid SQLAlchemy ORM issues
        result = db.execute(text("""
            INSERT INTO users (username, email, hashed_password, plan, is_active, created_at)
            VALUES (:username, :email, :hashed_password, :plan, :is_active, :created_at)
            RETURNING id
        """), {
            "username": username,
            "email": email, 
            "hashed_password": hashed_password,
            "plan": "free",
            "is_active": True,
            "created_at": datetime.datetime.utcnow()
        })
        
        user_id = result.fetchone()[0]
        db.commit()
        
        print(f"User registered successfully with ID: {user_id}")
        return RedirectResponse("/login", status_code=302)
        
    except Exception as e:
        db.rollback()
        error_msg = str(e)
        print(f"Registration error: {error_msg}")
        import traceback
        traceback.print_exc()
        
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Registration failed. Please try again."
        })
    finally:
        db.close()

# Also fix the get_user function to handle corrupted data
def get_user_safe(db, username: str):
    """Get user using raw SQL to avoid corrupted data issues"""
    try:
        from sqlalchemy import text
        result = db.execute(text("""
            SELECT id, username, email, plan, is_active, created_at,
                   stripe_customer_id, api_key, is_admin, role, industry, goals, posting_frequency
            FROM users 
            WHERE username = :username
        """), {"username": username})
        
        row = result.fetchone()
        if not row:
            return None
            
        # Create a user-like object
        class SafeUser:
            def __init__(self, row):
                self.id = row[0]
                self.username = row[1] 
                self.email = row[2]
                self.plan = row[3] or "free"
                self.is_active = row[4]
                self.created_at = row[5]
                self.stripe_customer_id = row[6]
                self.api_key = row[7]
                self.is_admin = row[8] or False
                self.role = row[9]
                self.industry = row[10]
                self.goals = row[11]
                self.posting_frequency = row[12]
                
        return SafeUser(row)
        
    except Exception as e:
        print(f"Error getting user safely: {e}")
        return None

# Updated authenticate_user_safe function
def authenticate_user_safe(db, username: str, password: str):
    """Authenticate user avoiding corrupted password data"""
    try:
        from sqlalchemy import text
        
        # Get user data including hashed password using raw SQL
        result = db.execute(text("""
            SELECT id, username, email, hashed_password, plan, is_active, created_at,
                   stripe_customer_id, api_key, is_admin, role, industry, goals, posting_frequency
            FROM users 
            WHERE username = :username
        """), {"username": username})
        
        row = result.fetchone()
        if not row:
            return None
        
        hashed_password = row[3]
        print(f"Retrieved password type: {type(hashed_password)}")
        
        # Handle different password storage formats
        if isinstance(hashed_password, str):
            # If stored as string, convert to bytes
            hashed_password = hashed_password.encode('utf-8')
        elif isinstance(hashed_password, memoryview):
            # Convert memoryview to bytes - THIS IS THE FIX
            hashed_password = bytes(hashed_password)
            print(f"Converted memoryview to bytes: {type(hashed_password)}")
        elif hashed_password is None:
            print("Password is None")
            return None
        
        # Verify password
        password_valid = verify_password(password, hashed_password)
        if not password_valid:
            return None
        
        # Create user object
        class SafeUser:
            def __init__(self, row):
                self.id = row[0]
                self.username = row[1]
                self.email = row[2] 
                self.plan = row[4] or "free"
                self.is_active = row[5]
                self.created_at = row[6]
                self.stripe_customer_id = row[7]
                self.api_key = row[8]
                self.is_admin = row[9] or False
                self.role = row[10]
                self.industry = row[11]
                self.goals = row[12]
                self.posting_frequency = row[13]
                
        return SafeUser(row)
        
    except Exception as e:
        print(f"Authentication error: {e}")
        import traceback
        traceback.print_exc()
        return None

# Updated verify_password function
def verify_password(plain_password: str, hashed_password: bytes) -> bool:
    """Verify a password against a hash"""
    try:
        print(f"verify_password called with hash type: {type(hashed_password)}")
        
        # Ensure plain_password is bytes
        if isinstance(plain_password, str):
            plain_password_bytes = plain_password.encode('utf-8')
        else:
            plain_password_bytes = plain_password
        
        # Handle case where hashed_password might be stored as string or memoryview
        if isinstance(hashed_password, str):
            print("WARNING: hashed_password is string, converting to bytes")
            hashed_password = hashed_password.encode('utf-8')
        elif isinstance(hashed_password, memoryview):
            print("Converting memoryview to bytes")
            hashed_password = bytes(hashed_password)
        
        return bcrypt.checkpw(plain_password_bytes, hashed_password)
        
    except Exception as e:
        print(f"Password verification error: {str(e)}")
        return False

# Updated hash_password function to ensure proper bytes handling
def hash_password(password: str) -> bytes:
    """Hash a password and return bytes for database storage"""
    try:
        print(f"hash_password called with password type: {type(password)}")
        
        # Ensure we're working with bytes for bcrypt
        if isinstance(password, str):
            password_bytes = password.encode('utf-8')
            print("Converted password string to bytes")
        else:
            password_bytes = password
            print("Password was already bytes")
        
        print("Generating salt...")
        salt = bcrypt.gensalt()
        print(f"Salt type: {type(salt)}")
        
        print("Hashing password...")
        hashed = bcrypt.hashpw(password_bytes, salt)
        print(f"bcrypt.hashpw returned type: {type(hashed)}")
        
        # bcrypt should always return bytes, but let's be extra safe
        if isinstance(hashed, str):
            print("WARNING: bcrypt returned string, converting to bytes")
            return hashed.encode('utf-8')
        
        if not isinstance(hashed, bytes):
            raise ValueError(f"bcrypt returned unexpected type: {type(hashed)}")
        
        print(f"Returning hashed password of type: {type(hashed)}")
        return hashed
        
    except Exception as e:
        print(f"Password hashing error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        raise ValueError(f"Failed to hash password: {str(e)}")

# Updated registration function with better error handling
@app.post("/register", response_class=HTMLResponse)
def register_user(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        print(f"Starting registration for username: {username}")
        
        # Use raw SQL to avoid SQLAlchemy processing corrupted data
        from sqlalchemy import text
        
        # Check username exists using raw SQL
        result = db.execute(text("""
            SELECT COUNT(*) FROM users WHERE username = :username
        """), {"username": username})
        username_count = result.scalar()
        
        if username_count > 0:
            print(f"Username {username} already exists")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Username already exists"
            })
        
        # Check email exists using raw SQL  
        result = db.execute(text("""
            SELECT COUNT(*) FROM users WHERE email = :email
        """), {"email": email})
        email_count = result.scalar()
        
        if email_count > 0:
            print(f"Email {email} already registered")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Email already registered"
            })
        
        # Hash the password
        print("Hashing password...")
        hashed_password = hash_password(password)
        print(f"Password hashed successfully, type: {type(hashed_password)}")
        
        # Ensure we have bytes for database storage
        if isinstance(hashed_password, memoryview):
            hashed_password = bytes(hashed_password)
        
        # Insert using raw SQL to avoid SQLAlchemy ORM issues
        result = db.execute(text("""
            INSERT INTO users (username, email, hashed_password, plan, is_active, created_at)
            VALUES (:username, :email, :hashed_password, :plan, :is_active, :created_at)
            RETURNING id
        """), {
            "username": username,
            "email": email, 
            "hashed_password": hashed_password,
            "plan": "free",
            "is_active": True,
            "created_at": datetime.datetime.utcnow()
        })
        
        user_id = result.fetchone()[0]
        db.commit()
        
        print(f"User registered successfully with ID: {user_id}")
        return RedirectResponse("/onboarding", status_code=302)
        
    except Exception as e:
        db.rollback()
        error_msg = str(e)
        print(f"Registration error: {error_msg}")
        import traceback
        traceback.print_exc()
        
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Registration failed. Please try again."
        })
    finally:
        db.close()

    # Check and create other tables if needed
    if 'generated_tweets' not in inspector.get_table_names():
        Base.metadata.tables["generated_tweets"].create(bind=engine)
    
    if 'team_members' not in inspector.get_table_names():
        Base.metadata.tables["team_members"].create(bind=engine)

# Alternative: Create a completely new User model to ensure proper types
class UserV2(Base):
    __tablename__ = "users_v2"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(LargeBinary, nullable=False)  # Ensure this is LargeBinary
    plan = Column(String, default="free")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    stripe_customer_id = Column(String, nullable=True)
    api_key = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    role = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    goals = Column(String, nullable=True)
    posting_frequency = Column(String, nullable=True)

# Function to migrate existing users to new table structure
def migrate_users_to_v2():
    """Migrate users from old table to new table with proper binary storage"""
    engine = create_engine(DATABASE_URL)
    
    # Create the new table
    UserV2.__table__.create(engine, checkfirst=True)
    
    db = SessionLocal()
    try:
        # Get all existing users
        old_users = db.query(User).all()
        
        for old_user in old_users:
            # Check if user already exists in v2 table
            existing = db.query(UserV2).filter(UserV2.username == old_user.username).first()
            if existing:
                continue
            
            # Handle the password - it might be stored as string in old table
            if isinstance(old_user.hashed_password, str):
                # If it's a bcrypt hash stored as string, encode it
                hashed_password = old_user.hashed_password.encode('utf-8')
            else:
                hashed_password = old_user.hashed_password
            
            new_user = UserV2(
                username=old_user.username,
                email=old_user.email,
                hashed_password=hashed_password,
                plan=getattr(old_user, 'plan', 'free'),
                is_active=old_user.is_active,
                created_at=old_user.created_at,
                stripe_customer_id=getattr(old_user, 'stripe_customer_id', None),
                api_key=getattr(old_user, 'api_key', None),
                is_admin=getattr(old_user, 'is_admin', False),
                role=getattr(old_user, 'role', None),
                industry=getattr(old_user, 'industry', None),
                goals=getattr(old_user, 'goals', None),
                posting_frequency=getattr(old_user, 'posting_frequency', None)
            )
            
            db.add(new_user)
        
        db.commit()
        print("Successfully migrated users to v2 table")
        
    except Exception as e:
        print(f"Migration error: {e}")
        db.rollback()
    finally:
        db.close()
    
    # Check and create other tables if needed
    if 'generated_tweets' not in inspector.get_table_names():
        Base.metadata.tables["generated_tweets"].create(bind=engine)
    
    if 'team_members' not in inspector.get_table_names():
        Base.metadata.tables["team_members"].create(bind=engine)
            
    # Check if stripe_customer_id column exists
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        if 'stripe_customer_id' not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR"))
            print("Added stripe_customer_id column to users table")
    
    # Check if plan column exists
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        if 'plan' not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'"))
            print("Added plan column to users table")
            
    # Check if generated_tweets table exists
    if 'generated_tweets' not in inspector.get_table_names():
        Base.metadata.tables["generated_tweets"].create(bind=engine)
        print("Created generated_tweets table")
    
    # Add API key column to users
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        if 'api_key' not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN api_key VARCHAR"))
            print("Added api_key column to users table")

    # Check if team_members table exists
    if 'team_members' not in inspector.get_table_names():
        Base.metadata.tables["team_members"].create(bind=engine)
        print("Created team_members table")

    # Add sequence reset logic at the end
    with engine.begin() as conn:
        # Reset sequences for all tables
        tables = ["users", "generated_tweets", "usage", "team_members"]
        for table in tables:
            try:
                # Get current max ID
                result = conn.execute(text(f"SELECT MAX(id) FROM {table}"))
                max_id = result.scalar() or 0
                
                # Reset sequence to next available ID
                conn.execute(
                    text(f"ALTER SEQUENCE {table}_id_seq RESTART WITH {max_id + 1}")
                )
                print(f"Reset sequence for {table} to {max_id + 1}")
            except Exception as e:
                print(f"Error resetting sequence for {table}: {str(e)}")

# Run migrations
migrate_database()

# ---- ROUTES ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/register", response_class=HTMLResponse)
def register(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("register.html", {"request": request, "user": user})

@app.post("/register", response_class=HTMLResponse)
def register_user(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        print(f"Starting registration for username: {username}")
        
        # Check if username exists
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            print(f"Username {username} already exists")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Username already exists"
            })
        
        # Check if email exists
        existing_email = db.query(User).filter(User.email == email).first()
        if existing_email:
            print(f"Email {email} already registered")
            return templates.TemplateResponse("register.html", {
                "request": request, 
                "error": "Email already registered"
            })
        
        # Hash the password with detailed debugging
        print("Starting password hashing...")
        hashed_password = hash_password(password)
        print(f"Hashed password type: {type(hashed_password)}")
        print(f"Hashed password length: {len(hashed_password) if hashed_password else 'None'}")
        
        # Double-check the type
        if not isinstance(hashed_password, bytes):
            print(f"ERROR: hashed_password is not bytes, it's {type(hashed_password)}")
            if isinstance(hashed_password, str):
                print("Converting string to bytes...")
                hashed_password = hashed_password.encode('utf-8')
            else:
                raise ValueError(f"Unexpected password hash type: {type(hashed_password)}")
        
        print("Creating User object...")
        user = User(
            username=username, 
            email=email, 
            hashed_password=hashed_password
        )
        
        print("Adding user to database...")
        db.add(user)
        
        print("Committing transaction...")
        db.commit()
        
        print("Registration successful!")
        return RedirectResponse("/onboarding", status_code=302)
        
    except Exception as e:
        db.rollback()
        error_msg = str(e)
        print(f"Registration error details: {error_msg}")
        print(f"Error type: {type(e)}")
        
        # Print the full traceback for debugging
        import traceback
        print("Full traceback:")
        traceback.print_exc()
        
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": f"Registration failed: {error_msg}"
        })
    finally:
        db.close()
        
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    user = get_optional_user(request)
    # If user is already logged in, redirect to dashboard
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "user": user})
    
@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        # Use the safe authentication method
        user = authenticate_user_safe(db, username, password)
        if not user:
            return templates.TemplateResponse("login.html", {
                "request": request, 
                "error": "Invalid credentials"
            })
        
        access_token = create_access_token(
            data={"sub": user.username},
            expires_delta=datetime.timedelta(days=2)
        )
        
        response = RedirectResponse("/dashboard", status_code=302)
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=True,  # Enable in production
            samesite='lax'
        )
        return response
    finally:
        db.close()

# Updated get_current_user function  
def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    
    if not token:
        raise credentials_exception
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    db = SessionLocal()
    try:
        # Use safe user retrieval
        user = get_user_safe(db, username)
        if user is None:
            raise credentials_exception
        
        # Attach features to the user object
        user.features = get_plan_features(user.plan)
        return user
    finally:
        db.close()

@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response

# email
# Add this function first (before your routes)
def send_email_sync(to_email: str, subject: str, body: str) -> bool:
    """Send email using SMTP configuration - synchronous version"""
    try:
        # Get email configuration from environment
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_username = os.getenv("SMTP_USERNAME")
        smtp_password = os.getenv("SMTP_PASSWORD")
        from_email = os.getenv("EMAIL_FROM")
        
        # Validate required environment variables
        if not all([smtp_server, smtp_username, smtp_password, from_email]):
            print("❌ Missing required email environment variables")
            print(f"SMTP_SERVER: {smtp_server}")
            print(f"SMTP_USERNAME: {smtp_username}")
            print(f"EMAIL_FROM: {from_email}")
            return False
        
        print(f"🔗 Connecting to SMTP server: {smtp_server}:{smtp_port}")
        print(f"👤 Username: {smtp_username}")
        print(f"📧 From email: {from_email}")
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = to_email
        
        # Add HTML body
        html_part = MIMEText(body, 'html')
        msg.attach(html_part)
        
        # Connect to server and send email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Enable security
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
            
        print(f"✅ Email sent successfully to {to_email}")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send email: {str(e)}")
        import traceback
        traceback.print_exc()
        return False
        
    
# Account Management Routes
@app.get("/account", response_class=HTMLResponse)
def account(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": user
    })

# Fix history route
@app.get("/history", response_class=HTMLResponse)
def tweet_history(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Use user.features instead of get_plan_features()
        days = user.features["history_days"]
        cutoff_date = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        
        tweets = db.query(GeneratedTweet).filter(
            GeneratedTweet.user_id == user.id,
            GeneratedTweet.generated_at >= cutoff_date
        ).order_by(GeneratedTweet.generated_at.desc()).all()
        
        return templates.TemplateResponse("history.html", {
            "request": request,
            "user": user,
            "tweets": tweets
        })
    finally:
        db.close()

@app.get("/team", response_class=HTMLResponse)
def team_management(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Use user.features instead of creating a separate features variable
        if user.features["team_seats"] <= 1:
            return RedirectResponse("/pricing", status_code=302)
        
        team_members = db.query(TeamMember).filter(
            TeamMember.user_id == user.id,
            TeamMember.status == "active"
        ).all()
        
        # Calculate values using user.features
        max_seats = user.features["team_seats"]
        available_seats = max_seats - len(team_members)
        
        return templates.TemplateResponse("team.html", {
            "request": request,
            "user": user,
            "team_members": team_members,
            "max_seats": max_seats,
            "available_seats": available_seats
        })
    finally:
        db.close()

@app.post("/account/change_password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    try:
        # We need to load the hashed_password for verification
        db_user = db.query(User).filter(User.id == user.id).first()
        
        if not verify_password(current_password, db_user.hashed_password):
            # Apply features to user object
            db_user = apply_plan_features(db_user)
            return templates.TemplateResponse("account.html", {
                "request": request,
                "user": db_user,
                "error": "Current password is incorrect"
            })

        # Update password
        db_user.hashed_password = hash_password(new_password)
        db.commit()
        
        # Apply features to user object
        db_user = apply_plan_features(db_user)
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": db_user,
            "success": "Password updated successfully!"
        })
    finally:
        db.close()

@app.post("/account/change_email")
async def change_email(
    request: Request,
    new_email: str = Form(...),
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    db_user = db.query(User).filter(User.id == user.id).first()

    if db.query(User).filter(User.email == new_email).first():
        db_user.features = get_plan_features(db_user.plan)   # ✅ PATCH
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": db_user,
            "features": db_user.features,                   # ✅ PATCH
            "error": "Email already in use"
        })

    db_user.email = new_email
    db.commit()

    db_user.features = get_plan_features(db_user.plan)       # ✅ PATCH
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": db_user,
        "features": db_user.features,                       # ✅ PATCH
        "success": "Email updated successfully!"
    })


@app.get("/api-docs", response_class=HTMLResponse)
def api_docs(request: Request, user: User = Depends(get_optional_user)):
    return templates.TemplateResponse("api_docs.html", {"request": request, "user": user})

@app.post("/account/delete")
async def delete_account(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    db_user = db.query(User).filter(User.id == user.id).first()
    
    # Delete user and their usage
    db.query(Usage).filter(Usage.user_id == user.id).delete()
    db.query(GeneratedTweet).filter(GeneratedTweet.user_id == user.id).delete()
    db.delete(db_user)
    db.commit()
    
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response
        
@app.get("/export-tweets")
def export_tweets(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        features = get_plan_features(user.plan)
        if not features["export"]:
            return Response(
                "Export feature not available for your plan",
                status_code=403,
                media_type="text/plain"
            )
        
        tweets = db.query(GeneratedTweet).filter(
            GeneratedTweet.user_id == user.id
        ).order_by(GeneratedTweet.generated_at.desc()).all()
        
        tweet_data = [{
            "text": tweet.tweet_text, 
            "date": tweet.generated_at.strftime("%Y-%m-%d %H:%M:%S")
        } for tweet in tweets]
        
        filename = f"tweets_export_{user.username}_{datetime.datetime.utcnow().strftime('%Y%m%d')}.json"
        
        return Response(
            content=json.dumps(tweet_data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    finally:
        db.close()

# Add routes
@app.post("/add-team-member")
async def add_team_member(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    try:
        features = get_plan_features(user.plan)
        current_team = db.query(TeamMember).filter(
            TeamMember.user_id == user.id,
            TeamMember.status == "active"
        ).all()
        
        if len(current_team) >= features["team_seats"]:
            return RedirectResponse("/team?error=No+available+seats", status_code=302)
            
        new_member = TeamMember(
            user_id=user.id,
            email=email,
            role=role
        )
        db.add(new_member)
        db.commit()
        
        # Send invitation email (pseudo-code)
        # send_invitation_email(email, user.username)
        
        return RedirectResponse("/team?success=Member+invited", status_code=302)
    except IntegrityError:
        return RedirectResponse("/team?error=Member+already+exists", status_code=302)
    finally:
        db.close()

@app.post("/remove-team-member")
async def remove_team_member(
    request: Request,
    email: str = Form(...),
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    try:
        member = db.query(TeamMember).filter(
            TeamMember.user_id == user.id,
            TeamMember.email == email
        ).first()
        
        if member:
            member.status = "removed"
            db.commit()
            return RedirectResponse("/team?success=Member+removed", status_code=302)
        
        return RedirectResponse("/team?error=Member+not+found", status_code=302)
    finally:
        db.close()

@app.get("/tweetgiver", response_class=HTMLResponse)
def tweetgiver(request: Request):
    user = get_optional_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("tweetgiver.html", {"request": request, "tweets": None, "user": user})

@app.post("/tweetgiver", response_class=HTMLResponse)
async def generate_tweetgiver(request: Request):
    user = None
    try: 
        user = get_current_user(request)
        # If user is logged in, redirect to dashboard for generation
        return RedirectResponse("/dashboard", status_code=302)
    except: 
        pass  # Allow non-authenticated users to generate once
        
    # Check if user has already used the playground (stored in session/cookie)
    playground_used = request.cookies.get("playground_used", "false")
    if playground_used == "true":
        # Redirect to registration if they've already used their one free try
        return RedirectResponse("/register", status_code=302)
        
    form = await request.form()
    job = form["job"]
    goal = form["goal"]
    
    # Generate tweets for non-authenticated users (no usage tracking)
    prompt = f"As a {job}, suggest 5 engaging tweets to achieve: {goal}."
    tweets = await get_ai_tweets(prompt, 5)
    
    response = templates.TemplateResponse("tweetgiver.html", {"request": request, "tweets": tweets, "user": user})
    # Set cookie to mark playground as used
    response.set_cookie(key="playground_used", value="true", max_age=86400)  # 24 hours
    return response

@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("pricing.html", {"request": request, "user": user})

@app.post("/generate-api-key")
async def generate_api_key(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        features = get_plan_features(user.plan)
        if not features["api_access"]:
            raise HTTPException(status_code=403, detail="API access not available for your plan")
        
        # Generate a secure API key
        new_key = secrets.token_urlsafe(32)
        user.api_key = new_key
        db.commit()
        
        return RedirectResponse("/account?success=API+key+generated", status_code=302)
    finally:
        db.close()

# Add a route to run the fix
@app.get("/fix-database")
async def fix_database_route():
    """Route to fix corrupted database data"""
    try:
        fix_corrupted_user_data()
        return {"status": "Database fix attempted. Check server logs."}
    except Exception as e:
        return {"status": f"Error: {str(e)}"}

@app.post("/cancel-subscription")
async def cancel_subscription(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    try:
        if not user.stripe_customer_id:
            # Try to find customer by email as fallback
            try:
                customers = stripe.Customer.list(email=user.email, limit=1)
                if customers.data:
                    user.stripe_customer_id = customers.data[0].id
                    db.commit()
                else:
                    return RedirectResponse(
                        "/account?error=No+Stripe+customer+found",
                        status_code=302
                    )
            except stripe.error.StripeError:
                return RedirectResponse(
                    "/account?error=Stripe+lookup+failed",
                    status_code=302
                )

        # Check if we already processed cancellation
        if user.plan == "canceling":
            return RedirectResponse(
                "/account?error=Subscription+already+canceled",
                status_code=302
            )

        # Get active subscriptions
        try:
            subscriptions = stripe.Subscription.list(
                customer=user.stripe_customer_id,
                status="active"
            )
        except stripe.error.StripeError as e:
            return RedirectResponse(
                f"/account?error=Stripe+API+Error%3A+{str(e).replace(' ', '+')}",
                status_code=302
            )

        if not subscriptions.data:
            return RedirectResponse(
                "/account?error=No+active+subscription+found",
                status_code=302
            )

        # Process cancellation
        try:
            for sub in subscriptions.data:
                stripe.Subscription.modify(
                    sub.id,
                    cancel_at_period_end=True
                )
            
            # Update user in database
            db_user = db.query(User).filter(User.id == user.id).first()
            db_user.plan = "canceling"
            db.commit()

            # Add verification task
            background_tasks.add_task(
                verify_cancellation,
                user.stripe_customer_id
            )

            return RedirectResponse(
                "/account?success=Subscription+will+cancel+at+period+end",
                status_code=302
            )
            
        except stripe.error.StripeError as e:
            db.rollback()
            return RedirectResponse(
                f"/account?error=Cancellation+failed%3A+{str(e).replace(' ', '+')}",
                status_code=302
            )

    except Exception as e:
        db.rollback()
        return RedirectResponse(
            f"/account?error=System+error%3A+{str(e).replace(' ', '+')}",
            status_code=302
        )
    finally:
        db.close()

async def verify_cancellation(stripe_customer_id: str):
    """Verify cancellation and downgrade when period ends"""
    db = SessionLocal()
    try:
        # Check every day until period ends
        while True:
            await asyncio.sleep(86400)  # 24 hours
            
            subscriptions = stripe.Subscription.list(
                customer=stripe_customer_id,
                status="all"
            )
            
            active_subs = [sub for sub in subscriptions.data if sub.status == "active"]
            
            # If no active subs, downgrade to free
            if not active_subs:
                user = db.query(User).filter(
                    User.stripe_customer_id == stripe_customer_id
                ).first()
                
                if user and user.plan == "canceling":
                    user.plan = "free"
                    db.commit()
                    print(f"Downgraded user {user.id} to free plan")
                    break
                
            # Check if any subscription is still active but ending soon
            ending_soon = False
            for sub in active_subs:
                if sub.cancel_at_period_end and sub.current_period_end > time.time():
                    ending_soon = True
                    break
                    
            if not ending_soon:
                break
                
    except Exception as e:
        print(f"Verification failed: {str(e)}")
    finally:
        db.close()

# Unified checkout endpoint for all plans
@app.post("/checkout/{plan_type}")
async def create_checkout_session(request: Request, plan_type: str):
    try:
        user = get_current_user(request)
    except HTTPException:
        return RedirectResponse("/register", status_code=302)

    try:
        # Map plan types to price IDs
        price_map = {
            "creator": STRIPE_CREATOR_PRICE_ID,
            "small_team": STRIPE_SMALL_TEAM_PRICE_ID,
            "agency": STRIPE_AGENCY_PRICE_ID,
            "enterprise": STRIPE_ENTERPRISE_PRICE_ID
        }
        
        price_id = price_map.get(plan_type)
        if not price_id:
            raise Exception("Invalid plan type")
            
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=str(request.url_for('checkout_success')) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=str(request.url_for('pricing')),
            customer_email=user.email,
            metadata={
                "user_id": user.id,
                "plan": plan_type  # Make sure this matches our plan names exactly
            }
        )
        return RedirectResponse(checkout_session.url, status_code=303)
    except Exception as e:
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": f"Error creating checkout session: {str(e)}",
            "user": user
        })

@app.post("/generate-tweet-api")
async def generate_tweet_api(
    request: Request,
    job: str = Form(...),
    goal: str = Form(...),
    api_key: str = Header(None)
):
    db = SessionLocal()
    try:
        if not api_key:
            raise HTTPException(status_code=401, detail="API key required")
        
        user = db.query(User).filter(User.api_key == api_key).first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API key")
        
        features = get_plan_features(user.plan)
        if not features["api_access"]:
            raise HTTPException(status_code=403, detail="API access not available for your plan")
        
        # Generate tweet using existing logic
        prompt = f"As a {job}, suggest an engaging tweet to achieve: {goal}."
        tweets = await get_ai_tweets(prompt, 1)
        
        # Save to history
        if tweets:
            generated_tweet = GeneratedTweet(
                user_id=user.id,
                tweet_text=tweets[0],
                generated_at=datetime.datetime.utcnow()
            )
            db.add(generated_tweet)
            db.commit()
            
            return {"tweet": tweets[0]}
        
        return {"error": "Failed to generate tweet"}
    finally:
        db.close()

# Update success handler to change user's plan
@app.get("/checkout/success")
async def checkout_success(request: Request, session_id: str = None):
    if not session_id or session_id == '{CHECKOUT_SESSION_ID}':
        return RedirectResponse("/pricing", status_code=302)

    plan_name = "Unknown Plan"
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        plan_name = session.metadata.get("plan", "Unknown Plan")
        
        # Update user's plan in database
        db = SessionLocal()
        try:
            user = get_optional_user(request)
            if user:
                db_user = db.query(User).filter(User.id == user.id).first()
                if db_user:
                    # Update plan and customer ID
                    db_user.plan = plan_name
                    
                    # Get customer ID from session
                    if session.customer:
                        db_user.stripe_customer_id = session.customer
                    
                    db.commit()
                    
                    # Refresh features
                    db_user.features = get_plan_features(plan_name)
        finally:
            db.close()
            
    except Exception as e:
        print(f"Error processing checkout: {str(e)}")

    # Get proper display name
    plan_display_names = {
        "creator": "Creator Plan",
        "small_team": "Small Team Plan",
        "agency": "Agency Plan",
        "enterprise": "Enterprise Plan"
    }
    
    display_name = plan_display_names.get(plan_name, 
        plan_name.replace("_", " ").title() + " Plan")

    return templates.TemplateResponse("checkout_success.html", {
        "request": request,
        "user": get_optional_user(request),
        "plan": display_name  # Pass the properly formatted plan name
    })
        
async def get_ai_tweets(prompt, count=5):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{
                "role": "system",
                "content": "You are a helpful AI assistant that generates tweets."
            },{
                "role": "user",
                "content": f"{prompt} Please generate exactly {count} tweets separated by new lines."
            }],
            max_tokens=500 + (count * 50),  # give more tokens if count increases
        )
        content = response.choices[0].message.content
        # Split on new lines but filter empty lines and numbering (1., 2., ...)
        tweets = [line.strip() for line in content.split('\n') if line.strip() and not line.strip().rstrip('.').isdigit()]
        # Trim or pad the number of tweets returned
        if len(tweets) < count:
            return tweets
        return tweets[:count]
    except Exception as e:
        return [f"Error: {str(e)}. Please check your OpenAI API key."]

@app.get("/register-success", response_class=HTMLResponse)
def register_success(request: Request):
    return templates.TemplateResponse("register_success.html", {"request": request})

@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_get(request: Request):
    return templates.TemplateResponse("onboarding.html", {"request": request})

@app.post("/onboarding", response_class=HTMLResponse)
def onboarding_post(request: Request,
                   role: str = Form(...),
                   industry: str = Form(...),
                   goals: str = Form(...),
                   posting_frequency: str = Form(...)):
    db = SessionLocal()
    user = get_optional_user(request)  # Or get_current_user if required
    if user:
        db_user = db.query(User).filter(User.id == user.id).first()
        db_user.role = role
        db_user.industry = industry
        db_user.goals = goals
        db_user.posting_frequency = posting_frequency
        db.commit()
    db.close()
    return RedirectResponse("/login", status_code=302)

@app.get("/onboarding-stats")
def onboarding_stats(request: Request, user: User = Depends(get_current_user)):
    if not user.is_admin:  # Add is_admin field/check as needed
        raise HTTPException(status_code=403)
    db = SessionLocal()
    stats = db.query(User.role, func.count(User.id)).group_by(User.role).all()
    db.close()
    # Render with chart data to your frontend template, or return JSON for charting on frontend
    return templates.TemplateResponse("onboarding_stats.html", {"request": request, "stats": stats})

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Check if user needs to complete onboarding
        needs_onboarding = not all([user.role, user.industry, user.goals, user.posting_frequency])
        
        if needs_onboarding:
            # Redirect to onboarding completion
            return RedirectResponse("/complete-onboarding", status_code=302)
        
        # Your existing dashboard logic
        today = str(datetime.date.today())
        usage = db.query(Usage).filter(
            Usage.user_id == user.id,
            Usage.date == today
        ).first()
        
        tweets_used = usage.count if usage else 0
        if user.plan != 'free':
            tweets_left = 'Unlimited'
        else:
            tweets_left = max(0, 15 - tweets_used)
        
        tweets = []
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "tweets": tweets,
            "tweets_left": tweets_left,
            "tweets_used": tweets_used,
            "features": user.features,
            "error": None
        })
    finally:
        db.close()

@app.post("/dashboard", response_class=HTMLResponse)
async def generate(request: Request):
    db = SessionLocal()
    try:
        user = get_current_user(request)
        form = await request.form()
        job = form.get("job")
        goal = form.get("goal")
        tweet_count_str = form.get("tweet_count", "1")
        try:
            tweet_count = int(tweet_count_str)
        except ValueError:
            tweet_count = 1  # fallback to 1 if invalid

        # Get usage for today
        today = str(datetime.date.today())
        usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
        if not usage:
            usage = Usage(user_id=user.id, date=today, count=0)
            db.add(usage)
            db.commit()

        # Calculate tweets left
        if user.plan == "free":
            max_allowed = 15
            tweets_left = max_allowed - usage.count
            if tweets_left <= 0:
                return templates.TemplateResponse("dashboard.html", {
                    "request": request,
                    "user": user,
                    "features": user.features,  # ADDED FEATURES HERE
                    "tweets_left": 0,
                    "tweets": [],
                    "error": "Daily limit reached! Upgrade for unlimited tweets."
                })
            if tweet_count > tweets_left:
                tweet_count = tweets_left

        # Build prompt with requested tweet count
        prompt = f"As a {job}, suggest {tweet_count} engaging tweets to achieve: {goal}."

        tweets = await get_ai_tweets(prompt, count=tweet_count)

        # Save generated tweets to history
        for tweet_text in tweets:
            generated_tweet = GeneratedTweet(
                user_id=user.id,
                tweet_text=tweet_text,
                generated_at=datetime.datetime.utcnow()
            )
            db.add(generated_tweet)

        # Update usage count
        usage.count += len(tweets)
        db.commit()

        # Calculate remaining tweets for free tier
        if user.plan == "free":
            new_tweets_left = max(0, max_allowed - usage.count)
        else:
            new_tweets_left = "Unlimited"

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "features": user.features,  # ADDED FEATURES HERE
            "tweets": tweets,
            "tweets_left": new_tweets_left,
            "tweets_used": usage.count,
            "error": None
        })
    finally:
        db.close()
        
# temporary test route to the FastAPI app (remove after testing)
@app.get("/test-email")
def test_email_sync():
    """Test email sending with immediate response"""
    print("=== EMAIL CONFIGURATION DEBUG ===")
    print(f"SMTP_SERVER: {os.getenv('SMTP_SERVER')}")
    print(f"SMTP_PORT: {os.getenv('SMTP_PORT')}")
    print(f"SMTP_USERNAME: {os.getenv('SMTP_USERNAME')}")
    print(f"SMTP_PASSWORD: {'✓ Set' if os.getenv('SMTP_PASSWORD') else '❌ Missing'}")
    print(f"EMAIL_FROM: {os.getenv('EMAIL_FROM')}")
    print("=================================")
    
    success = send_email_sync(
        to_email="egs001102@gmail.com", 
        subject="TEST Email from Giver.ai",
        body="<h1>This is a test</h1><p>If you see this, email sending works!</p>"
    )
    
    return {
        "message": "Test email sent" if success else "Email failed",
        "success": success,
        "smtp_server": os.getenv('SMTP_SERVER'),
        "smtp_port": os.getenv('SMTP_PORT')
    }

@app.get("/test-email-background")
async def test_email_background(background_tasks: BackgroundTasks):
    """Test email sending with background task"""
    print("=== QUEUEING EMAIL IN BACKGROUND ===")
    
    def email_task():
        print("🚀 Background email task starting...")
        success = send_email_sync(
            to_email="egs001102@gmail.com", 
            subject="Background TEST Email from Giver.ai",
            body="<h1>Background Task Test</h1><p>This email was sent via background task!</p>"
        )
        print(f"📧 Background email task completed: {success}")
    
    background_tasks.add_task(email_task)
    return {"message": "Email queued in background - check server logs!"}

@app.get("/debug-email")
def debug_email():
    """Debug email configuration without sending"""
    debug_info = {
        "environment_variables": {
            "SMTP_SERVER": os.getenv("SMTP_SERVER"),
            "SMTP_PORT": os.getenv("SMTP_PORT"), 
            "SMTP_USERNAME": os.getenv("SMTP_USERNAME"),
            "SMTP_PASSWORD": "***SET***" if os.getenv("SMTP_PASSWORD") else "❌ MISSING",
            "EMAIL_FROM": os.getenv("EMAIL_FROM")
        },
        "render_env_check": "Looking for SMTP_* and EMAIL_* vars...",
        "found_email_vars": {k: v for k, v in os.environ.items() if 'SMTP' in k or 'EMAIL' in k}
    }
    
    # Test SMTP connection without sending
    try:
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_username = os.getenv("SMTP_USERNAME") 
        smtp_password = os.getenv("SMTP_PASSWORD")
        
        if not all([smtp_server, smtp_username, smtp_password]):
            debug_info["smtp_test"] = "❌ SKIPPED - Missing credentials"
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_username, smtp_password)
                debug_info["smtp_test"] = "✅ SUCCESS - SMTP connection works!"
                
    except Exception as e:
        debug_info["smtp_test"] = f"❌ FAILED: {str(e)}"
@app.post("/complete-onboarding")
def complete_onboarding_post(request: Request,
                            role: str = Form(...),
                            industry: str = Form(...),
                            goals: str = Form(...),
                            posting_frequency: str = Form(...),
                            user: User = Depends(get_current_user)):
    """Save the missing onboarding data"""
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.role = role
            db_user.industry = industry
            db_user.goals = goals
            db_user.posting_frequency = posting_frequency
            db.commit()
            print(f"Completed onboarding for existing user: {db_user.username}")
            
        return RedirectResponse("/dashboard", status_code=302)
    finally:
        db.close()

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        # Check if client accepts HTML
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=302)
    # Default JSON response for API calls
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle subscription events
    if event['type'] == 'customer.subscription.updated':
        subscription = event['data']['object']
        if subscription.cancel_at_period_end:
            background_tasks.add_task(
                handle_subscription_cancellation,
                subscription.customer
            )

    # FIXED: Proper indentation for this block
    if event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        background_tasks.add_task(
            downgrade_user_plan,
            subscription.customer
        )
    
    return {"status": "success"}
    
async def handle_subscription_cancellation(stripe_customer_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            User.stripe_customer_id == stripe_customer_id
        ).first()
        if user:
            user.plan = "canceling"
            db.commit()
    finally:
        db.close()
        
async def downgrade_user_plan(stripe_customer_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            User.stripe_customer_id == stripe_customer_id
        ).first()
        
        if user and user.plan == "canceling":
            user.plan = "free"
            db.commit()
            print(f"Downgraded user {user.id} via webhook")
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
