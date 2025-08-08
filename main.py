import datetime
import os
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy import LargeBinary
from sqlalchemy import inspect
from openai import OpenAI
from pydantic import BaseModel
from jose import JWTError, jwt
import stripe

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
DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ----- User & Usage Models -----
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

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    date = Column(String)
    count = Column(Integer, default=0)
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
    return db.query(User).filter(User.username == username).first()

def authenticate_user(db, username: str, password: str):
    user = get_user(db, username)
    if not user or not verify_password(password, user.hashed_password): 
        return None
    return user

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
        
        # Refresh the user object to ensure it's not detached
        db.refresh(user)
        return user
    finally:
        db.close()

def get_optional_user(request: Request):
    try:
        return get_current_user(request)
    except Exception:
        return None

# ---- FastAPI Setup -----
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY", "your-openrouter-api-key-here")
)
DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

Base.metadata.create_all(bind=engine)

def migrate_database():
    from sqlalchemy import text  # Add this import
    
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    inspector = inspect(engine)
    
    # Check if stripe_customer_id column exists
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        if 'stripe_customer_id' not in columns:
            with engine.begin() as conn:
                # Wrap SQL in text() construct
                conn.execute(text("ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR"))
            print("Added stripe_customer_id column to users table")
    
    # Check if plan column exists
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        if 'plan' not in columns:
            with engine.begin() as conn:
                # Wrap SQL in text() construct
                conn.execute(text("ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'"))
            print("Added plan column to users table")

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
    return templates.TemplateResponse("account.html", {"request": request, "user": user})

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
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": user,
            "error": "Current password is incorrect"
        })
    
    db_user.hashed_password = get_password_hash(new_password)
    db.commit()
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": db_user,
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
    
    # Check if email already exists
    if db.query(User).filter(User.email == new_email).first():
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": user,
            "error": "Email already in use"
        })
    
    db_user.email = new_email
    db.commit()
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": db_user,
        "success": "Email updated successfully!"
    })

@app.post("/account/delete")
async def delete_account(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    db_user = db.query(User).filter(User.id == user.id).first()
    
    # Delete user and their usage
    db.query(Usage).filter(Usage.user_id == user.id).delete()
    db.delete(db_user)
    db.commit()
    
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response
    
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
                "plan": plan_type
            }
        )
        return RedirectResponse(checkout_session.url, status_code=303)
    except Exception as e:
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": f"Error creating checkout session: {str(e)}",
            "user": user
        })

# Update success handler to change user's plan
@app.get("/checkout/success")
async def checkout_success(request: Request, session_id: str = None):
    if not session_id or session_id == '{CHECKOUT_SESSION_ID}':
        return RedirectResponse("/pricing", status_code=302)

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        plan = session.metadata.get("plan")
        
        if plan:
            # Update user's plan in database
            db = SessionLocal()
            try:
                user = get_optional_user(request)
                if user:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    if db_user:
                        db_user.plan = plan
                        db.commit()
            finally:
                db.close()
                
        return templates.TemplateResponse("checkout_success.html", {
            "request": request,
            "session": session,
            "user": get_optional_user(request),
            "plan": plan.replace("_", " ").title() if plan else "Unknown"
        })
    except Exception as e:
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": f"Error retrieving session: {str(e)}",
            "user": get_optional_user(request)
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
def onboarding_post(request: Request, role: str = Form(...), industry: str = Form(...), goals: str = Form(...), posting_frequency: str = Form(...)):
    # For now, just redirect to login since we don't have user session yet
    # In a real app, you'd save this data to the user's profile
    return RedirectResponse("/login", status_code=302)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    db = SessionLocal()
    try:
        user = get_current_user(request)
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
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "tweets_left": tweets_left,
            "tweets_used": tweets_used
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
                    "tweets_left": 0,
                    "tweets": [],
                    "error": "Daily limit reached! Upgrade for unlimited tweets."
                })
            if tweet_count > tweets_left:
                tweet_count = tweets_left
        else:
            tweets_left = "Unlimited"

        # Build prompt with requested tweet count
        prompt = f"As a {job}, suggest {tweet_count} engaging tweets to achieve: {goal}."

        tweets = await get_ai_tweets(prompt, count=tweet_count)

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
            "tweets": tweets,
            "tweets_left": new_tweets_left,
            "tweets_used": usage.count,
            "error": None
        })
    finally:
        db.close()

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET")
        )
    except ValueError as e:
        raise HTTPException(status_code=400)
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400)

    # Handle subscription events
    if event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        user_id = subscription.metadata.get("user_id")
        if user_id:
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    # Downgrade to free plan if subscription canceled
                    user.plan = "free"
                    db.commit()
            finally:
                db.close()

    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
