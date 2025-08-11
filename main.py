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
from sqlalchemy.exc import IntegrityError
from openai import OpenAI
from pydantic import BaseModel
from jose import JWTError, jwt
import stripe
import json
import asyncio

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

def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())

def verify_password(plain_password: str, hashed_password: bytes) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password)

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
    hashed_password = Column(LargeBinary) 
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

Base.metadata.create_all(bind=engine)

# ----- Auth Setup -----
SECRET_KEY = "your-secret-key-here"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

def get_password_hash(password: str) -> bytes:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

def verify_password(plain_password: str, hashed_password: bytes) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password)

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
        return db.query(User).filter(User.username == username).first()
    except ProgrammingError as e:
        if "column users.is_admin does not exist" in str(e):
            migrate_database()  # Auto-fix missing column
            return db.query(User).filter(User.username == username).first()
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

def migrate_database():
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
       # Check and add all missing columns
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        
        # List of all columns that should exist
        required_columns = {
            'is_admin': "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE",
            'api_key': "ALTER TABLE users ADD COLUMN api_key VARCHAR",
            'stripe_customer_id': "ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR",
            'plan': "ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'",
            # ADD THE MISSING COLUMNS HERE
            'role': "ALTER TABLE users ADD COLUMN role VARCHAR",
            'industry': "ALTER TABLE users ADD COLUMN industry VARCHAR",
            'goals': "ALTER TABLE users ADD COLUMN goals VARCHAR",
            'posting_frequency': "ALTER TABLE users ADD COLUMN posting_frequency VARCHAR"
        }
        
        for col_name, sql in required_columns.items():
            if col_name not in columns:
                with engine.begin() as conn:
                    conn.execute(text(sql))
                print(f"Added {col_name} column to users table")
    
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
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username already exists"})
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"})
    user = User(username=username, email=email, hashed_password=get_password_hash(password))
    db.add(user)
    db.commit()
    return RedirectResponse("/onboarding", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        user = authenticate_user(db, username, password)
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

@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response
    
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
    db_user = db.query(User).filter(User.id == user.id).first()

    if not verify_password(current_password, db_user.hashed_password):
        # Ensure features here too
        db_user.features = get_plan_features(db_user.plan)
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": db_user,
            "features": db_user.features,
            "error": "Current password is incorrect"
        })

    db_user.hashed_password = get_password_hash(new_password)
    db.commit()
    db_user.features = get_plan_features(db_user.plan)  # ✅ Added line
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": db_user,
        "features": db_user.features,
        "success": "Password updated successfully!"
    })


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

@app.get("/team", response_class=HTMLResponse)
def team_management(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        features = get_plan_features(user.plan)
        if features["team_seats"] <= 1:
            return RedirectResponse("/pricing", status_code=302)
        
        # In a real app, you'd fetch actual team members
        team_members = [
            {"email": "member1@example.com", "role": "Editor"},
            {"email": "member2@example.com", "role": "Viewer"}
        ]
        
        return templates.TemplateResponse("team.html", {
            "request": request,
            "user": user,
            "team_members": team_members,
            "max_seats": features["team_seats"],
            "available_seats": features["team_seats"] - len(team_members)
        })
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
        
        # Initialize tweets as an empty list since we're not generating any yet
        tweets = []
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "tweets": tweets,  # Now properly defined
            "tweets_left": tweets_left,  # Fixed variable name (was new_tweets_left)
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
