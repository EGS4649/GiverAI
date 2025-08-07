from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, func, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy import LargeBinary
from openai import OpenAI
import datetime, os
from jose import JWTError, jwt
import stripe
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
    plan = Column(String, default="free")  # or "creator"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    date = Column(String)
    count = Column(Integer, default=0)
    user = relationship("User")

Base.metadata.create_all(bind=engine)

# ----- Auth Setup -----
SECRET_KEY = "your-secret-key-here"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

# ----- Stripe Setup -----
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_your_stripe_secret_key_here")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_test_your_stripe_publishable_key_here")

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
    if not user or not verify_password(password, user.hashed_password): return None
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
    user = get_user(db, username=username)
    if user is None:
        raise credentials_exception
    return user

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
    db.add(user); db.commit()
    return RedirectResponse("/onboarding", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    access_token = create_access_token(data={"sub": user.username}, expires_delta=datetime.timedelta(days=2))
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(key="access_token", value=access_token, httponly=True)
    return response

@app.get("/logout")
def logout():
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

@app.post("/checkout/creator")
async def create_checkout_session(request: Request):
    # Require login to ensure user email known
    try:
        user = get_current_user(request)
    except HTTPException:
        return RedirectResponse("/register", status_code=302)

    try:
        # Use your Stripe price ID here
        price_id = os.getenv("STRIPE_CREATOR_PRICE_ID")  # Set in your environment variables

        if not price_id:
            raise Exception("Stripe price ID is not set in environment variables")

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
          success_url = request.url_for('checkout_success').include_query_params(session_id='{CHECKOUT_SESSION_ID}')
cancel_url = request.url_for('pricing')
            customer_email=user.email  # Using user email for payment receipt, etc.
        )
        return RedirectResponse(checkout_session.url, status_code=303)
    except Exception as e:
        # You can add logging here as needed
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": f"Error creating checkout session: {str(e)}",
            "user": user
        })

@app.get("/checkout/success")
async def checkout_success(request: Request, session_id: str = None):
    if not session_id:
        return RedirectResponse("/pricing", status_code=302)

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        # Optional: Verify session and subscription status here
        customer_email = session.customer_details.email

        # Update user plan in your DB
        db = SessionLocal()
        user = db.query(User).filter(User.email == customer_email).first()
        if user:
            user.plan = "creator"
            db.commit()

        return templates.TemplateResponse("checkout_success.html", {
            "request": request,
            "session": session,
            "user": user,
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
    user = get_current_user(request)
    db = SessionLocal()
    today = str(datetime.date.today())
    usage = db.query(Usage).filter(Usage.user_id==user.id, Usage.date==today).first()
    tweets_used = usage.count if usage else 0
    tweets_left = max(0, 15 - tweets_used) if user.plan == "free" else 'Unlimited'
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "tweets_left": tweets_left, "tweets_used": tweets_used})

@app.post("/dashboard", response_class=HTMLResponse)
async def generate(request: Request):
    user = get_current_user(request)
    db = SessionLocal()

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

    # Calculate tweets left (for 'free' user) or unlimited
    if user.plan.lower() == "free":
        max_allowed = 15
        tweets_left = max_allowed - usage.count
        if tweets_left <= 0:
            # User has no credits left; reject request immediately
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "tweets_left": 0,
                "tweets": [],
                "error": "Daily limit reached! Upgrade for unlimited tweets."
            })

        # Cap tweet_count to available credits
        if tweet_count > tweets_left:
            # Optionally show an error or just reduce count
            tweet_count = tweets_left
    else:
        tweets_left = "Unlimited"
        # No cap for paid users

    # Build prompt with requested tweet count
    prompt = f"As a {job}, suggest {tweet_count} engaging tweets to achieve: {goal}."

    tweets = await get_ai_tweets(prompt, count=tweet_count)

    # Important: Increment usage count by the number of tweets actually generated
    # To be safe, cap increment by tweet_count and/or number of tweets returned
    usage.count += min(tweet_count, len(tweets))
    db.commit()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "tweets": tweets,
        "tweets_left": tweets_left - min(tweet_count, len(tweets)) if isinstance(tweets_left, int) else tweets_left,
        "error": None
    })

    
    # Generate tweets
    prompt = f"As a {job}, suggest engaging tweets to achieve: {goal}."
    tweets = await get_ai_tweets(prompt, tweet_count)
    
    # Update usage - add the actual number of tweets generated
    usage.count += len(tweets)
    db.commit()
    
    # Calculate remaining tweets
    new_tweets_used = usage.count
    tweets_left = max(0, 15 - new_tweets_used) if user.plan == "free" else 'Unlimited'
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request, 
        "user": user, 
        "tweets_left": tweets_left,
        "tweets_used": new_tweets_used,
        "tweets": tweets
    })



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
