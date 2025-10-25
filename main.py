import datetime
from datetime import datetime, time, timedelta, date, timezone
import os
import re
import secrets
import requests 
import hashlib
from typing import Optional, List
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, BackgroundTasks, Header, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text, Text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from email_validator import validate_email, EmailNotValidError
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from sqlalchemy import LargeBinary
from sqlalchemy import inspect
from sqlalchemy import Text 
from sqlalchemy.exc import IntegrityError, ProgrammingError 
from sqlalchemy.orm import defer
from sqlalchemy import func, Text
from sqlalchemy import Text, TIMESTAMP
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from openai import OpenAI
from jose import JWTError, jwt
import stripe
import json
import asyncio
import smtplib
import pytz
import ipaddress
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import logging
import structlog
import bleach

IS_PRODUCTION = os.getenv("ENV", "development") == "production"

# Add your reCAPTCHA variables here
RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY")
RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY")

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_CREATOR_PRICE_ID = os.getenv("STRIPE_CREATOR_PRICE_ID")
STRIPE_SMALL_TEAM_PRICE_ID = os.getenv("STRIPE_SMALL_TEAM_PRICE_ID")
STRIPE_AGENCY_PRICE_ID = os.getenv("STRIPE_AGENCY_PRICE_ID")
STRIPE_ENTERPRISE_PRICE_ID = os.getenv("STRIPE_ENTERPRISE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Configure structured logging
logging.basicConfig(level=logging.INFO)
logger = structlog.get_logger()

try:
    import bcrypt
    print("bcrypt module imported successfully")
except Exception as e:
    print("bcrypt import error:", e)

try:
    from zoneinfo import ZoneInfo
    TIMEZONE_AVAILABLE = True
    
except ImportError:
    # Fallback for older Python versions
    try:
        import pytz
        TIMEZONE_AVAILABLE = True
    except ImportError:
        TIMEZONE_AVAILABLE = False
        print("⚠️ Neither zoneinfo nor pytz available. Timestamps will be shown in UTC.")

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
    generated_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")

class IPban(Base):
    __tablename__ = "ip_bans"
    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String, index=True)
    reason = Column(Text, nullable=True)
    banned_at = Column(DateTime, default=datetime.utcnow)
    banned_by = Column(String, nullable=True)  # admin who banned the IP
    expires_at = Column(DateTime, nullable=True)  # null = permanent ban
    is_active = Column(Boolean, default=True)
    unbanned_at = Column(DateTime, nullable=True)
    unbanned_by = Column(String, nullable=True)

class SuspensionAppeal(Base):
    __tablename__ = "suspension_appeals"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    appeal_type = Column(String, nullable=False)
    appeal_message = Column(Text, nullable=False)
    status = Column(String, default="pending")  # pending, approved, denied
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)
    admin_notes = Column(Text, nullable=True)
    user = relationship("User")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(LargeBinary, nullable=False)
    plan = Column(String, default="free")  # Now supports: free, creator, small_team, agency, enterprise
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    stripe_customer_id = Column(String, nullable=True)  
    api_key = Column(String, nullable=True)  # Added API key column
    is_admin = Column(Boolean, default=False)
    role = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    goals = Column(String, nullable=True)
    posting_frequency = Column(String, nullable=True)
    original_plan = Column(String, nullable=True)
    is_suspended = Column(Boolean, default=False)
    suspension_reason = Column(String, nullable=True)
    suspended_at = Column(DateTime, nullable=True)
    suspended_by = Column(String, nullable=True) 
    last_login = Column(DateTime, nullable=True)
    last_known_ip = Column(String, nullable=True)
    registration_ip = Column(String, nullable=True)
    is_ip_banned = Column(Boolean, default=False)
    cancellation_date = Column(DateTime, nullable=True)
    cancellation_requested_at = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, default=False)

    # Security logging
    last_password_change = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, default=0)
    last_failed_login = Column(DateTime, nullable=True)
    account_locked_until = Column(DateTime, nullable=True)

class Usage(Base):
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    date = Column(String)
    count = Column(Integer, default=0)
    user = relationship("User")
    
class UserActivity(Base):
    __tablename__ = "user_activity"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    activity_type = Column(String)  # 'login', 'tweet_generated', 'plan_change', etc.
    description = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_metadata = Column(String, nullable=True) 
    user = relationship("User")
    
class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)
    used = Column(Boolean, default=False)
    used_at = Column(DateTime, nullable=True)
    user = relationship("User")

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    email = Column(String, index=True)
    role = Column(String, default="editor")
    status = Column(String, default="pending")  # pending, active, removed
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")

# Security Headers Middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://www.google.com https://www.gstatic.com https://cdnjs.cloudflare.com https://js.stripe.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data: https:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self' https://api.stripe.com; "
            "frame-src https://www.google.com https://js.stripe.com; "
            "object-src 'none';"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

class EmailService:
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.smtp_username = os.getenv("SMTP_USERNAME")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("EMAIL_FROM", "noreply@giverai.me")
        self.sender_name = os.getenv("EMAIL_SENDER_NAME", "GiverAI")
       
    async def send_email(self, to_email: str, subject: str, body: str):
        """Base email sending method"""
        try:
            print(f"📧 Attempting to send email to: {to_email}")
            print(f"📧 Subject: {subject}")
            
            # Create message
            msg = MIMEMultipart()
            msg['From'] = "noreply@giverai.me"
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Add body
            msg.attach(MIMEText(body, 'plain'))
            
            # Send email
            print(f"🔌 Connecting to SMTP server: {self.smtp_server}:{self.smtp_port}")
            
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()  # Enable security
            
            print(f"🔐 Logging in with username: {self.smtp_username}")
            server.login(self.smtp_username, self.smtp_password)
            
            print(f"📤 Sending email...")
            text = msg.as_string()
            server.sendmail(self.from_email, to_email, text)
            server.quit()
            
            print(f"✅ Email sent successfully to {to_email}")
            
        except Exception as e:
            print(f"❌ Failed to send email to {to_email}: {e}")
            print(f"❌ Error type: {type(e).__name__}")
            import traceback
            print(f"❌ Full traceback: {traceback.format_exc()}")
            raise e

    def send_simple_email(self, to_email: str, subject: str, html_body: str) -> bool:
        """Send an email with simple HTML"""
        try:
            if not all([self.smtp_server, self.smtp_username, self.smtp_password]):
                print("⛔ Missing email configuration")
                return False

            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{self.sender_name} <{self.from_email}>"
            msg["To"] = to_email

            html_part = MIMEText(html_body, "html")
            msg.attach(html_part)

            # Send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)

            print(f"✅ Email sent to {to_email}")
            return True

        except Exception as e:
            print(f"⛔ Failed to send email: {str(e)}")
            return False
        
    async def send_password_reset_email(self, user, reset_token, ip_address="Unknown"):
        """Send password reset email"""
        reset_url = f"https://giverai.me/reset-password?token={reset_token}"

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #667eea;">Password Reset Request</h1>
              <p>Hi {user.username}!</p>
              <p>We received a request to reset your password for your GiverAI account.</p>
              <p>
                <a href="{reset_url}"
                   style="background: #28a745; color: white; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px;
                          display: inline-block;">
                  Reset My Password
                </a>
              </p>
              <p>This link expires in 1 hour.</p>
              <p>Request made from IP: {ip_address}</p>
              <p>If you didn't request this, please ignore this email.</p>
              <p>Best regards,<br>The GiverAI Team</p>
            </div>
          </body>
        </html>
        """
        
        return self.send_simple_email(
            user.email,
            "Reset Your GiverAI Password 🔑",
            html_body
        )

    async def send_account_locked_email(self, email: str, lock_duration_hours: int = 24):
        """Send account locked notification email"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #dc3545;">Account Temporarily Locked</h1>
              <p>Your GiverAI account has been temporarily locked due to multiple failed login attempts.</p>
              <p><strong>Lock Duration:</strong> {lock_duration_hours} hours</p>
              <p>This is a security measure to protect your account. You can try logging in again after the lock period expires.</p>
              <p>If you believe this was not you, please contact our support team immediately.</p>
              <p>Best regards,<br>The GiverAI Team</p>
            </div>
          </body>
        </html>
        """
        
        return self.send_simple_email(
            email,
            "Account Temporarily Locked - GiverAI",
            html_body
        )
        
    async def send_suspension_email(self, email: str, reason: str):
        """Send account suspension notification email"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #dc3545;">Account Suspended</h1>
              <p>Your GiverAI account has been suspended.</p>
              <p><strong>Reason:</strong> {reason}</p>
              <p>If you believe this was done in error, please contact our support team.</p>
              <p>Best regards,<br>The GiverAI Team</p>
            </div>
          </body>
        </html>
        """
        
        return self.send_simple_email(
            email,
            "Account Suspended - GiverAI",
            html_body
        )
        
    async def send_account_recovery_email(self, email: str):
        """Send account recovery instructions email"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #ffc107;">Account Security Alert</h1>
              <p>We've received a report that your account may have been compromised.</p>
              <p><strong>Immediate actions taken:</strong></p>
              <ul>
                <li>Account temporarily suspended</li>
                <li>All active sessions invalidated</li>
                <li>Password reset required</li>
              </ul>
              <p>To recover your account, please contact our support team with proof of identity.</p>
              <p>Best regards,<br>The GiverAI Team</p>
            </div>
          </body>
        </html>
        """
        
        return self.send_simple_email(
            email,
            "Account Security Alert - GiverAI",
            html_body
        )
    async def send_downgrade_confirmation_email(self, user):
        pass
        """Send downgrade confirmation email"""

    async def send_verification_email(self, user, verification_token):
        """Send verification email with simple template"""
        verification_url = f"https://giverai.me/verify-email?token={verification_token}"

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #667eea;">Verify Your Email</h1>
              <p>Hi {user.username}!</p>
              <p>Please verify your email address by clicking the button below:</p>
              <p>
                <a href="{verification_url}"
                   style="background: #28a745; color: white; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px;
                          display: inline-block;">
                  Verify Email Address
                </a>
              </p>
              <p>This link expires in 24 hours.</p>
              <p>Best regards,<br>The GiverAI Team</p>
            </div>
          </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            "Verify Your GiverAI Account",
            html_body
        )
    async def send_welcome_email(self, user):
        """Send welcome email to new user"""
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
             <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                  <div style="background: #667eea; color: white; padding: 30px;
                  text-align: center; border-radius: 8px;">
                    <h1 style="margin: 0; color: white;">Welcome to GiverAI! 🎉</h1>
                        <p style="margin: 10px 0 0 0; color: white;">
                      Your AI-powered Twitter content creation platform
                        </p>
                      </div>

              <div style="padding: 30px; background: white; border: 1px solid #eee;
                      border-radius: 0 0 8px 8px;">
            <h2 style="color: #333;">Hi {user.username}! 👋</h2>
            <p>
              We're thrilled to have you join our community of content creators
              who are transforming their Twitter presence with AI.
            </p>

            <div style="background: #f8f9fa; padding: 15px; margin: 15px 0;
                        border-radius: 6px; border-left: 4px solid #667eea;">
              <h3 style="margin-top: 0; color: #333;">✨ Your Free Plan Includes:</h3>
              <ul style="margin: 0; padding-left: 20px;">
                <li>15 AI-generated tweets per day</li>
                <li>Basic customization options</li>
                <li>1-day tweet history</li>
              </ul>
            </div>

            <p>Ready to create your first viral tweet?</p>
            <p style="text-align: center;">
              <a href="https://giverai.me/dashboard"
                 style="display: inline-block; background: #667eea;
                        color: white; padding: 12px 24px;
                        text-decoration: none; border-radius: 6px;">
                Start Creating Tweets
              </a>
            </p>

            <p>Happy tweeting!</p>
            <p><strong>The GiverAI Team</strong></p>
          </div>

          <div style="text-align: center; margin-top: 20px; color: #666;
                      font-size: 12px;">
            <p>GiverAI - AI-Powered Twitter Content Creation</p>
          </div>

        </div>
      </body>
    </html>
    """

        return self.send_simple_email(
            user.email,
            "Welcome to GiverAI! Your Twitter Content Creation Journey Starts Now 🚀",
            html_body,
        )
        
    async def send_subscription_upgrade_email(self, user, old_plan, new_plan, amount, next_billing_date):
        """Send subscription upgrade notification."""
        plan_features = get_plan_features(new_plan)
        feature_list = []

        # Build feature list dynamically
        if plan_features["daily_limit"] == float("inf"):
            feature_list.append("• Unlimited daily tweets")
        else:
            feature_list.append(f"• {plan_features['daily_limit']} tweets per day")

        if plan_features["team_seats"] > 1:
            feature_list.append(f"• {plan_features['team_seats']} team seats")

        if plan_features["export"]:
            feature_list.append("• Export tweet history")

        if plan_features["analytics"]:
            feature_list.append("• Advanced analytics")

        if plan_features["api_access"]:
            feature_list.append("• API access")

        plan_descriptions = {
            "creator": "Perfect for individual creators and influencers",
            "small_team": "Ideal for small teams and growing businesses",
            "agency": "Built for agencies managing multiple clients",
            "enterprise": "Complete solution for large organizations",
        }

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; color: white;">Subscription Upgraded! 🎉</h1>
                <p style="margin: 10px 0 0 0; color: white;">Welcome to {new_plan.replace('_', ' ').title()}</p>
              </div>

              <div style="background: white; padding: 30px; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2 style="color: #333;">Hi {user.username}!</h2>

                <div style="background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 20px; margin: 20px 0; border-radius: 8px; text-align: center;">
                  <h2 style="margin: 0; color: white;">🚀 Welcome to {new_plan.replace('_', ' ').title()}!</h2>
                  <p style="margin: 10px 0 0 0; color: white;">{plan_descriptions.get(new_plan, '')}</p>
                </div>

                <h3>🎯 Your New Features:</h3>
                <div style="background: #f8f9fa; padding: 20px; margin: 15px 0; border-radius: 6px;">
                  {"<br>".join(feature_list)}
                </div>

                <h3>💳 Billing Details:</h3>
                <div style="background: #e8f5e8; padding: 15px; margin: 15px 0; border-radius: 6px;">
                  <p><strong>Previous Plan:</strong> {old_plan.replace('_', ' ').title()}</p>
                  <p><strong>New Plan:</strong> {new_plan.replace('_', ' ').title()}</p>
                  <p><strong>Amount:</strong> ${amount}/month</p>
                  <p><strong>Next Billing Date:</strong> {next_billing_date.strftime('%Y-%m-%d') if next_billing_date else 'N/A'}</p>
                </div>

                <p>Your new features are active immediately! Start exploring them now.</p>

                <p style="text-align: center;">
                  <a href="https://giverai.me/dashboard" 
                     style="display: inline-block; background: #667eea; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                    Explore New Features
                  </a>
                </p>

                <p>Questions about your subscription? Visit your <a href="https://giverai.me/account">account settings</a> or contact support.</p>

                <p>Thanks for upgrading and supporting GiverAI!</p>
                <p><strong>The GiverAI Team</strong></p>
              </div>

              <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
                <p>GiverAI - AI-Powered Twitter Content Creation</p>
                <p>Manage your subscription: <a href="https://giverai.me/account">Account Settings</a></p>
              </div>
            </div>
          </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            f"Welcome to {new_plan.replace('_', ' ').title()}! Your GiverAI Upgrade is Active 🚀",
            html_body,
        )

    def send_subscription_cancellation_email(self, user, original_plan, cancellation_date):
        """Send subscription cancellation notification."""
    
        plan_display_names = {
            "creator": "Creator",
            "small_team": "Small Team",
            "agency": "Agency",
            "enterprise": "Enterprise",
        }

        plan_name = plan_display_names.get(original_plan, original_plan.replace("_", " ").title())
        print(f"📧 Email using plan name: {plan_name} (from original_plan: {original_plan})")

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              
              <!-- Header Section -->
              <div style="background: #dc3545; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; color: white;">Subscription Cancelled 😢</h1>
                <p style="margin: 10px 0 0 0; color: white;">We're sorry to see you go</p>
              </div>

              <!-- Main Body -->
              <div style="background: white; padding: 30px; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2 style="color: #333;">Hi {user.username},</h2>

                <div style="background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; margin: 20px 0; border-radius: 6px;">
                  <p style="margin: 0;">
                    <strong>⚠️ Your {plan_name} subscription has been cancelled</strong>
                  </p>
                </div>

                <p>
                  Your subscription will remain active until 
                  <strong>{cancellation_date.strftime('%B %d, %Y') if cancellation_date else 'the end of your billing period'}</strong>.
                  After that, your account will be downgraded to the free plan.
                </p>

                <h3>What happens next?</h3>
                <ul>
                  <li>✅ Continue using all premium features until 
                       {cancellation_date.strftime('%B %d, %Y') if cancellation_date else 'your period ends'}</li>
                  <li>📅 No more charges after your current period ends</li>
                  <li>🔄 Automatic downgrade to free plan on 
                       {cancellation_date.strftime('%B %d, %Y') if cancellation_date else 'your end date'}</li>
                </ul>

                <h3>Changed your mind?</h3>
                <p>
                  You can reactivate your subscription anytime before 
                  {cancellation_date.strftime('%B %d, %Y') if cancellation_date else 'your period ends'}.
                </p>

                <p style="text-align: center;">
                  <a href="https://giverai.me/account" 
                     style="display: inline-block; background: #28a745; color: white; 
                            padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                    Reactivate Subscription
                  </a>
                </p>

                <p>Thanks for being part of GiverAI. We hope to see you again!</p>
                <p><strong>The GiverAI Team</strong></p>
              </div>
            </div>
          </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            f"Your {plan_name} Subscription Has Been Cancelled",
            html_body,
        )
    
    def send_subscription_downgrade_email(self, user, old_plan):
        """Send notification when user is downgraded to free plan"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <div style="background: #6c757d; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; color: white;">Plan Downgraded ⬇️</h1>
                <p style="margin: 10px 0 0 0; color: white;">You're now on the Free Plan</p>
                  </div>

          <div style="background: white; padding: 30px; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
            <h2 style="color: #333;">Hi {user.username},</h2>

            <div style="background: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; margin: 20px 0; border-radius: 6px;">
              <p style="margin: 0;"><strong>ℹ️ Your subscription period has ended</strong></p>
              <p>Your {old_plan.replace('_', ' ').title()} plan has expired and you've been moved to our Free Plan.</p>
            </div>

            <h3>🎯 Your Current Free Plan Features:</h3>
            <div style="background: #f8f9fa; padding: 20px; margin: 15px 0; border-radius: 6px;">
              <p>• 15 AI-generated tweets per day</p>
              <p>• Basic customization options</p>
              <p>• 1-day tweet history</p>
              <p>• Community support</p>
            </div>

            <h3>💡 Want More?</h3>
            <p>Upgrade anytime to get back your premium features:</p>
            <div style="background: #fff3cd; padding: 15px; margin: 15px 0; border-radius: 6px;">
              <p><strong>Creator Plan:</strong> Unlimited tweets, 60-day history, advanced customization</p>
              <p><strong>Small Team:</strong> Team collaboration + all Creator features</p>
              <p><strong>Agency:</strong> Advanced analytics + white label options</p>
            </div>

            <div style="text-align: center; margin: 30px 0;">
              <a href="https://giverai.me/pricing" 
                 style="display: inline-block; background: #667eea; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                View Pricing Plans
              </a>
            </div>

            <p><strong>Good News:</strong> All your tweet history and account data are safe and will be restored when you upgrade!</p>

            <p>Questions? We're here to help at support@giverai.me</p>

            <p>Thanks for being part of GiverAI!</p>
            <p><strong>The GiverAI Team</strong></p>
          </div>

          <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
            <p>GiverAI - AI-Powered Twitter Content Creation</p>
          </div>
        </div>
      </body>
    </html>
        """
        return self.send_simple_email(
            user.email,
            "Your GiverAI Plan Has Changed to Free Plan",
            html_body
        )

    def send_username_reminder_email(self, user):
        """Send username reminder email"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #667eea;">Username Reminder</h1>
              <p>Hi there!</p>
              <p>You requested a reminder of your GiverAI username. Here it is:</p>
              <div style="background: #e3f2fd; padding: 20px; margin: 20px 0; text-align: center; border-radius: 8px;">
                <h2 style="color: #667eea; margin: 10px 0;">{user.username}</h2>
              </div>
              <p>
            <a href="https://giverai.me/login"
               style="background: #667eea; color: white; padding: 12px 24px;
                      text-decoration: none; border-radius: 4px;
                      display: inline-block;">
              Log In to GiverAI
            </a>
          </p>
          <p>Plan: {user.plan.replace('_', ' ').title()}</p>
          <p>Member since: {user.created_at.strftime('%B %d, %Y')}</p>
          <p>If you also forgot your password, you can <a href="https://giverai.me/forgot-password">reset it here</a>.</p>
          <p>Best regards,<br>The GiverAI Team</p>
        </div>
      </body>
    </html>
        """

        return self.send_simple_email(
            user.email,
            "Your GiverAI Username Reminder 👤",
            html_body
        )

    def send_password_reset_success_email(self, user, ip_address="Unknown"):
        """Send password reset success confirmation"""
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <h1 style="color: #28a745;">Password Changed Successfully!</h1>
              <p>Hi {user.username}!</p>
              <p>Your GiverAI password has been successfully changed.</p>
              <div style="background: #d4edda; padding: 15px; margin: 20px 0; border-radius: 6px;">
                <p><strong>When:</strong> {datetime.utcnow().strftime('%B %d, %Y at %I:%M %p UTC')}</p>
                <p><strong>IP Address:</strong> {ip_address}</p>
            </div>
              <p>If you didn't change your password, please contact our support team immediately.</p>
              <p>Best regards,<br>The GiverAI Security Team</p>
            </div>
          </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            "Your GiverAI Password Has Been Changed ✅",
            html_body
        )

    def send_account_changed_email(self, user, change_details, ip_address="Unknown"):
        """Send account security alert"""
        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: #dc3545; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                    <h1 style="margin: 0; color: white;">Account Security Alert 🔒</h1>
                    <p style="margin: 10px 0 0 0; color: white;">Important changes to your GiverAI account</p>
                </div>

                <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                    <h2 style="color: #333;">Hi {user.username},</h2>

                    <div style="background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; margin: 20px 0; border-radius: 6px;">
                        <p style="margin: 0;"><strong>⚠️ Your account information was recently updated</strong></p>
                    </div>

                    <div style="background: #f8f9fa; padding: 15px; margin: 15px 0; border-radius: 6px;">
                        <h3 style="margin-top: 0; color: #333;">What Changed:</h3>
                        <p style="margin: 5px 0;">{change_details}</p>
                        <p style="margin: 5px 0;"><strong>When:</strong> {datetime.now().strftime("%B %d, %Y at %I:%M %p UTC")}</p>
                        <p style="margin: 5px 0;"><strong>IP Address:</strong> {ip_address}</p>
                    </div>

                    <h3>Was this you?</h3>
                <p>If you made this change, no action is needed. Your account is secure.</p>

                    <div style="background: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; margin: 20px 0; border-radius: 6px;">
                    <p style="margin: 0;"><strong>🚨 If you didn't authorize this change:</strong></p>
                    <ol style="margin: 10px 0; padding-left: 20px;">
                        <li>Contact our support team immediately at support@giverai.me</li>
                        <li>Change your password as soon as possible</li>
                        <li>Review your account for any other unauthorized changes</li>
                    </ol>
                </div>

                <p>Your account security is our priority. If you have any concerns, please don't hesitate to contact us.</p>

                <p>Best regards,<br><strong>The GiverAI Security Team</strong></p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            "Important Changes to Your GiverAI Account",
            html_body
        )


    def send_contact_form_notification(self, name: str, email: str, subject_category: str, message: str, user_info: str = None):
        """Send contact form submission to support email"""

        subject_labels = {
            "general_support": "General Support",
            "technical_issue": "Technical Issue", 
            "billing_question": "Billing Question",
            "feature_request": "Feature Request",
            "account_help": "Account Help",
            "bug_report": "Bug Report",
            "feedback": "Feedback",
            "other": "Other"
        }
    
        subject_label = subject_labels.get(subject_category, subject_category)
        formatted_message = message.replace('\n', '<br>')
    
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
            <div style="background: #667eea; color: white; padding: 20px; text-align: center;">
                <h1 style="margin: 0; color: white;">New Contact Form Submission 📧</h1>
                <p style="margin: 10px 0 0 0; color: white;">From your GiverAI website</p>
            </div>
    
            <div style="padding: 30px; background: white; border: 1px solid #eee;">
                <h2 style="color: #333; border-bottom: 2px solid #667eea; padding-bottom: 10px;">Contact Details</h2>
    
                <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                    <tr style="background: #f8f9fa;">
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold; width: 30%;">Name:</td>
                        <td style="padding: 12px; border: 1px solid #dee2e6;">{name}</td>
                    </tr>
                    <tr>
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Email:</td>
                        <td style="padding: 12px; border: 1px solid #dee2e6;">
                            <a href="mailto:{email}" style="color: #667eea;">{email}</a>
                        </td>
                    </tr>
                    <tr style="background: #f8f9fa;">
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Category:</td>
                        <td style="padding: 12px; border: 1px solid #dee2e6;">
                            <span style="background: #667eea; color: white; padding: 4px 12px; border-radius: 15px; font-size: 12px;">
                                {subject_label}
                            </span>
                        </td>
                    </tr>
                    {f'''<tr>
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Account Info:</td>
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-size: 14px; color: #666;">{user_info}</td>
                    </tr>''' if user_info else ''}
                    <tr style="background: #f8f9fa;">
                        <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Timestamp:</td>
                        <td style="padding: 12px; border: 1px solid #dee2e6;">{datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}</td>
                    </tr>
                </table>
    
                <h3 style="color: #333; margin-top: 30px;">Message:</h3>
                <div style="background: #f8f9fa; padding: 20px; border-left: 4px solid #667eea; border-radius: 4px; line-height: 1.6;">
                    {formatted_message}
                </div>
    
                <div style="margin-top: 30px; padding: 20px; background: #e3f2fd; border-radius: 8px;">
                    <p style="margin: 0; color: #1976d2;">
                        <strong>💡 Quick Reply:</strong> Simply reply to this email to respond directly to {name} at {email}
                    </p>
                </div>
            </div>
    
            <div style="background: #f8f9fa; padding: 20px; text-align: center; color: #666; font-size: 14px;">
                <p>This message was sent via the GiverAI contact form</p>
            </div>
        </body>
        </html>
        """
    
        return self.send_simple_email(
            "support@giverai.me",
            f"[GiverAI Contact] {subject_label} - {name}",
            html_body
        )
    def send_contact_confirmation_email(self, name: str, email: str, subject_category: str):
        """Send confirmation email to user who submitted contact form"""
    
        subject_labels = {
            "general_support": "General Support",
            "technical_issue": "Technical Issue",
            "billing_question": "Billing Question", 
            "feature_request": "Feature Request",
            "account_help": "Account Help",
            "bug_report": "Bug Report",
            "feedback": "Feedback",
            "partnership": "Partnership Inquiry",
            "other": "Other"
        }
        
        subject_label = subject_labels.get(subject_category, subject_category)
        
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <div style="background: #28a745; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                <h1 style="margin: 0; color: white;">Message Received! ✅</h1>
                <p style="margin: 10px 0 0 0; color: white;">We'll get back to you soon</p>
              </div>
              
              <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2 style="color: #333;">Hi {name}!</h2>
                
                <p>Thanks for reaching out to GiverAI! We've received your message about <strong>{subject_label}</strong> and we'll respond as soon as possible.</p>
                
                <div style="background: #e3f2fd; padding: 15px; margin: 20px 0; border-radius: 6px;">
                  <p style="margin: 0;"><strong>📧 What's next?</strong></p>
                  <ul style="margin: 10px 0; padding-left: 20px;">
                    <li>We typically respond within 24 hours</li>
                    <li>Priority support users get faster responses</li>
                    <li>You'll receive our reply at {email}</li>
                  </ul>
                </div>
                
                <p>In the meantime, you might find answers in our <a href="https://giverai.me/faq" style="color: #667eea;">FAQ section</a> or feel free to continue using GiverAI!</p>
                
                <p style="text-align: center; margin: 30px 0;">
                  <a href="https://giverai.me/dashboard" 
                     style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                    Back to GiverAI
                  </a>
                </p>
                
                <p>Thanks for being part of GiverAI!</p>
                <p><strong>The GiverAI Support Team</strong></p>
              </div>
            </div>
          </body>
        </html>
        """
        
        return self.send_simple_email(
            email,
            "We received your message - GiverAI Support",
            html_body
        )


    def send_contact_form_confirmation(self, name: str, email: str, subject_category: str):
        """Send confirmation email to user who submitted contact form"""

        subject_labels = {
            "general_support": "General Support",
            "technical_issue": "Technical Issue",
            "billing_question": "Billing Question", 
            "feature_request": "Feature Request",
            "account_help": "Account Help",
            "bug_report": "Bug Report",
            "feedback": "Feedback",
            "other": "Other"
        }
    
        subject_label = subject_labels.get(subject_category, subject_category)
    
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: #28a745; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                    <h1 style="margin: 0; color: white;">Message Received! ✅</h1>
                    <p style="margin: 10px 0 0 0; color: white;">We'll get back to you soon</p>
                </div>
    
                <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                    <h2 style="color: #333;">Hi {name}!</h2>
    
                    <p>Thanks for reaching out to GiverAI! We've received your message about <strong>{subject_label}</strong> and we'll respond as soon as possible.</p>
    
                    <div style="background: #e3f2fd; padding: 15px; margin: 20px 0; border-radius: 6px;">
                        <p style="margin: 0;"><strong>📧 What's next?</strong></p>
                        <ul style="margin: 10px 0; padding-left: 20px;">
                            <li>We typically respond within 24 hours</li>
                            <li>Priority support users get faster responses</li>
                            <li>You'll receive our reply at {email}</li>
                        </ul>
                    </div>
    
                    <p>In the meantime, you might find answers in our <a href="https://giverai.me/faq" style="color: #667eea;">FAQ section</a> or feel free to continue using GiverAI!</p>
    
                    <p style="text-align: center; margin: 30px 0;">
                        <a href="https://giverai.me/dashboard" 
                           style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                            Back to GiverAI
                        </a>
                    </p>
    
                    <p>Thanks for being part of GiverAI!</p>
                    <p><strong>The GiverAI Support Team</strong></p>
                </div>
            </div>
        </body>
        </html>
        """
    
        return self.send_simple_email(
            email,
            "We received your message - GiverAI Support",
            html_body
        )

    def send_email_change_verification(self, user, new_email, verification_token, ip_address="Unknown"):
        """Send email verification for email change to the new email address"""
        verification_url = f"https://giverai.me/verify-email-change?token={verification_token}"

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
              <div style="background: #667eea; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                <h1 style="margin: 0; color: white;">Verify Your New Email Address</h1>
                <p style="margin: 10px 0 0 0; color: white;">Complete your email change request</p>
              </div>
    
              <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2 style="color: #333;">Hi {user.username}!</h2>
    
                <div style="background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; margin: 20px 0; border-radius: 6px;">
                  <p style="margin: 0;"><strong>📧 You requested to change your email address</strong></p>
                </div>
    
                <p>To complete the email change process, please verify this new email address by clicking the button below:</p>
    
                <div style="background: #f8f9fa; padding: 15px; margin: 15px 0; border-radius: 6px;">
                  <p style="margin: 5px 0;"><strong>New Email:</strong> {new_email}</p>
                  <p style="margin: 5px 0;"><strong>Request Time:</strong> {datetime.now().strftime("%B %d, %Y at %I:%M %p UTC")}</p>
                  <p style="margin: 5px 0;"><strong>IP Address:</strong> {ip_address}</p>
                </div>
    
                <p style="text-align: center;">
                  <a href="{verification_url}"
                     style="display: inline-block; background: #28a745; color: white; 
                            padding: 15px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                    Verify New Email Address
                  </a>
                </p>
    
                <p><strong>Important:</strong> This verification link expires in 1 hour.</p>
    
                <div style="background: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; margin: 20px 0; border-radius: 6px;">
                  <p style="margin: 0;"><strong>🚨 If you didn't request this change:</strong></p>
                  <p>Someone may be trying to access your account. Please contact our support team immediately at support@giverai.me</p>
                </div>
    
                <p>After verification, all future notifications will be sent to this email address.</p>
    
                <p>Best regards,<br><strong>The GiverAI Team</strong></p>
              </div>
    
              <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
                <p>This verification was sent to: {new_email}</p>
              </div>
            </div>
          </body>
        </html>
        """
    
        return self.send_simple_email(
            new_email,
            "Verify Your New Email Address - GiverAI",
            html_body
        )
    
    def send_email_changed_notification(self, user, old_email, ip_address="Unknown"):
        """Send notification to old email address when email is changed"""
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: #dc3545; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                    <h1 style="margin: 0; color: white;">Email Address Changed 🔒</h1>
                    <p style="margin: 10px 0 0 0; color: white;">Your GiverAI account email was updated</p>
                </div>
    
                <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                    <h2 style="color: #333;">Hi {user.username},</h2>
    
                    <div style="background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; margin: 20px 0; border-radius: 6px;">
                        <p style="margin: 0;"><strong>⚠️ Your account email address was recently changed</strong></p>
                    </div>
    
                    <div style="background: #f8f9fa; padding: 15px; margin: 15px 0; border-radius: 6px;">
                        <h3 style="margin-top: 0; color: #333;">Change Details:</h3>
                        <p style="margin: 5px 0;"><strong>From:</strong> {old_email}</p>
                        <p style="margin: 5px 0;"><strong>To:</strong> {user.email}</p>
                        <p style="margin: 5px 0;"><strong>When:</strong> {datetime.now().strftime("%B %d, %Y at %I:%M %p UTC")}</p>
                        <p style="margin: 5px 0;"><strong>IP Address:</strong> {ip_address}</p>
                    </div>
    
                    <h3>Was this you?</h3>
                    <p>If you made this change, no action is needed. Future notifications will be sent to your new email address.</p>
    
                    <div style="background: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; margin: 20px 0; border-radius: 6px;">
                        <p style="margin: 0;"><strong>🚨 If you didn't authorize this change:</strong></p>
                        <ol style="margin: 10px 0; padding-left: 20px;">
                            <li>Contact our support team immediately at support@giverai.me</li>
                            <li>Change your password as soon as possible</li>
                            <li>Review your account for any other unauthorized changes</li>
                        </ol>
                    </div>
    
                    <p>Your account security is our priority. If you have any concerns, please don't hesitate to contact us.</p>
    
                    <p>Best regards,<br><strong>The GiverAI Security Team</strong></p>
                </div>
    
                <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
                    <p>This notification was sent to your previous email address: {old_email}</p>
                </div>
            </div>
        </body>
        </html>
        """
    
        return self.send_simple_email(
            old_email,
            "Your GiverAI Email Address Was Changed 📧",
            html_body
        )
    def send_goodbye_email(self, user, total_tweets, days_active, plan):

        plan_display_names = {
        "creator": "Creator",
        "small_team": "Small Team", 
        "agency": "Agency",
        "enterprise": "Enterprise",
        "free": "Free",
        "canceling": "Free"  
    }

        plan_name = plan_display_names.get(plan, plan.replace("_", " ").title())
    
        """Send account deletion confirmation"""
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: #f5576c; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                <h1 style="margin: 0; color: white;">Account Deleted 😢</h1>
                <p style="margin: 10px 0 0 0; color: white;">We're sorry to see you go, {user.username}</p>
            </div>

            <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2 style="color: #333;">Your GiverAI account has been successfully deleted</h2>

                <div style="background: #fff3cd; padding: 15px; margin: 20px 0; border-radius: 6px; border: 1px solid #ffeaa7;">
                    <h3 style="margin-top: 0; color: #333;">🗂️ Data Removal</h3>
                    <p>As requested, we have permanently deleted:</p>
                    <ul style="margin: 0; padding-left: 20px;">
                        <li>Your account profile and settings</li>
                        <li>All generated tweets and content history</li>
                        <li>Usage data and analytics</li>
                        <li>Team memberships and collaborations</li>
                        <li>Billing and subscription information</li>
                    </ul>
                    <p><strong>This action cannot be undone.</strong></p>
                </div>

                <h3>Thank You for Using GiverAI</h3>
                <p>We appreciate the time you spent with us. During your journey, you:</p>
                <ul style="padding-left: 20px;">
                    <li>📝 Generated {total_tweets} tweets</li>
                    <li>📅 Were with us for {days_active} days</li>
                    <li>🎯 Used the {plan_name} plan</li>
                </ul>

                <h3>Changed Your Mind?</h3>
                <p>You're always welcome back! If you decide to return, you can create a new account anytime, though your previous data cannot be restored.</p>

                <p style="text-align: center;">
                    <a href="https://giverai.me/register" 
                       style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                        Return to GiverAI
                    </a>
                </p>

                <p>We hope our paths cross again in the future. Until then, we wish you all the best with your content creation journey!</p>

                <p>Farewell and best wishes,</p>
                <p><strong>The GiverAI Team</strong></p>
            </div>

            <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
                <p>This confirmation was sent to {user.email}</p>
                <p>You will not receive any further emails from us.</p>
            </div>
        </div>
    </body>
    </html>
    """

        return self.send_simple_email(
            user.email,
            "We're Sorry to See You Go - Your GiverAI Account Has Been Deleted 👋",
            html_body
        )
    
async def send_suspension_appeal_notification(self, user, appeal):
        """Send suspension appeal notification to admin"""
        appeal_types = {
        "wrongful_suspension": "Account Wrongfully Suspended",
        "policy_misunderstanding": "Policy Misunderstanding", 
        "technical_error": "Technical Error",
        "account_compromise": "Account Was Compromised",
        "content_misidentified": "Content Misidentified",
        "first_time_offense": "First Time Offense - Request Leniency",
        "other": "Other"
    }
    
        appeal_type_display = appeal_types.get(appeal.appeal_type, appeal.appeal_type)
    
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: #dc3545; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                <h1 style="margin: 0; color: white;">Suspension Appeal Submitted 📋</h1>
                <p style="margin: 10px 0 0 0; color: white;">Requires Admin Review</p>
            </div>
            
            <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                <h2>Appeal Details</h2>
                
                <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                <tr style="background: #f8f9fa;">
                    <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">User:</td>
                    <td style="padding: 12px; border: 1px solid #dee2e6;">{user.username} ({user.email})</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Appeal Type:</td>
                    <td style="padding: 12px; border: 1px solid #dee2e6;">{appeal_type_display}</td>
                </tr>
                <tr style="background: #f8f9fa;">
                    <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Suspended Date:</td>
                    <td style="padding: 12px; border: 1px solid #dee2e6;">
                    {user.suspended_at.strftime('%Y-%m-%d %H:%M UTC') if user.suspended_at else 'Unknown'}
                    </td>
                </tr>
                <tr>
                    <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Suspension Reason:</td>
                    <td style="padding: 12px; border: 1px solid #dee2e6;">{user.suspension_reason}</td>
                </tr>
                <tr style="background: #f8f9fa;">
                    <td style="padding: 12px; border: 1px solid #dee2e6; font-weight: bold;">Plan:</td>
                    <td style="padding: 12px; border: 1px solid #dee2e6;">{user.plan.replace('_', ' ').title()}</td>
                </tr>
                </table>
                
                <h3>Appeal Message:</h3>
                <div style="background: #f8f9fa; padding: 20px; border-left: 4px solid #dc3545; border-radius: 4px;">
                {appeal.appeal_message.replace(chr(10), '<br>')}
                </div>
                
                <p style="text-align: center; margin: 30px 0;">
                <a href="https://giverai.me/admin/appeals" 
                    style="display: inline-block; background: #dc3545; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px;">
                    Review Appeal
                </a>
                </p>
            </div>
            </div>
        </body>
        </html>
        """
        
        return self.send_simple_email(
            "support@giverai.me",
            f"[URGENT] Suspension Appeal from {user.username} - {appeal_type_display}",
            html_body
        )
    
async def send_appeal_confirmation_email(self, user, appeal_type):
        """Send confirmation email to user who submitted appeal"""
        
        appeal_type_labels = {
            "wrongful_suspension": "Wrongful Suspension",
            "policy_misunderstanding": "Policy Misunderstanding", 
            "technical_error": "Technical Error",
            "account_compromise": "Account Compromised",
            "content_misidentified": "Content Misidentified",
            "first_time_offense": "First Time Offense",
            "other": "Other"
        }
        
        appeal_label = appeal_type_labels.get(appeal_type, appeal_type)
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: #28a745; color: white; padding: 30px; text-align: center; border-radius: 8px;">
                    <h1 style="margin: 0; color: white;">Appeal Received ✅</h1>
                    <p style="margin: 10px 0 0 0; color: white;">We're reviewing your suspension appeal</p>
                </div>
    
                <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
                    <h2 style="color: #333;">Hi {user.username}!</h2>
    
                    <p>Thank you for submitting your suspension appeal. We've received your request to review the suspension of your GiverAI account.</p>
    
                    <div style="background: #e3f2fd; padding: 15px; margin: 20px 0; border-radius: 6px;">
                        <p style="margin: 0;"><strong>📋 Appeal Details:</strong></p>
                        <ul style="margin: 10px 0; padding-left: 20px;">
                            <li><strong>Type:</strong> {appeal_label}</li>
                            <li><strong>Submitted:</strong> {datetime.now().strftime("%B %d, %Y at %I:%M %p UTC")}</li>
                            <li>
                                <strong>Status:</strong> Under Review
                            </li>
                        </ul>
                    </div>

                    <p>
                        Our team will carefully review your appeal and may reach out if we need more information.<br>
                        You'll receive a follow-up email within <strong>24-48 hours</strong> with our decision.
                    </p>

                    <p>If you have further questions, reply to this email or contact <a href="mailto:support@giverai.me">support@giverai.me</a>.</p>
                    
                    <div style="background: #fff3cd; padding: 15px; border-radius: 6px; margin-top: 20px;">
                        <p style="margin: 0;">
                            <strong>Need urgent assistance?</strong> Please reply with any additional context or requests for expedited review.
                        </p>
                    </div>

                    <p style="margin-top: 30px; color: #888; font-size: 13px;">
                        This confirmation was automatically generated by GiverAI Suspension Appeals.<br>
                        Reference ID: <strong>{user.id}</strong>
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_simple_email(
            user.email,
            "✅ GiverAI Suspension Appeal Received",
            html_body
        )
    
def send_appeal_confirmation_email(self, user, appeal):
    """Send appeal confirmation to user"""
    appeal_types = {
        "wrongful_suspension": "Account Wrongfully Suspended",
        "policy_misunderstanding": "Policy Misunderstanding",
        "technical_error": "Technical Error", 
        "account_compromise": "Account Was Compromised",
        "content_misidentified": "Content Misidentified",
        "first_time_offense": "First Time Offense - Request Leniency",
        "other": "Other"
    }
    
    appeal_type_display = appeal_types.get(appeal.appeal_type, appeal.appeal_type)
    
    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
          <div style="background: #28a745; color: white; padding: 30px; text-align: center; border-radius: 8px;">
            <h1 style="margin: 0; color: white;">Appeal Submitted Successfully ✅</h1>
            <p style="margin: 10px 0 0 0; color: white;">We've received your suspension appeal</p>
          </div>
          
          <div style="padding: 30px; background: white; border: 1px solid #eee; border-radius: 0 0 8px 8px;">
            <h2>Hi {user.username},</h2>
            
            <p>Thank you for submitting your suspension appeal. We've received your request and our team will review it carefully.</p>
            
            <div style="background: #e3f2fd; padding: 15px; margin: 20px 0; border-radius: 6px;">
              <h3 style="margin-top: 0;">Your Appeal Details:</h3>
              <p><strong>Appeal Type:</strong> {appeal_type_display}</p>
              <p><strong>Submitted:</strong> {appeal.created_at.strftime('%B %d, %Y at %I:%M %p UTC')}</p>
              <p><strong>Reference ID:</strong> #{appeal.id}</p>
            </div>
            
            <h3>What happens next?</h3>
            <ul>
              <li>📋 Our team will review your appeal within 24-48 hours</li>
              <li>🔍 We'll investigate the circumstances of your suspension</li>
              <li>📧 You'll receive an email with our decision</li>
              <li>✅ If approved, your account will be restored immediately</li>
            </ul>
            
            <div style="background: #fff3cd; padding: 15px; margin: 20px 0; border-radius: 6px;">
              <p style="margin: 0;"><strong>⏰ Average Review Time:</strong> 24-48 hours</p>
              <p>Complex cases may require additional investigation time.</p>
            </div>
            
            <p>Please don't submit additional appeals as this may delay the review process.</p>
            
            <p>Thank you for your patience,</p>
            <p><strong>The GiverAI Appeals Team</strong></p>
          </div>
        </div>
      </body>
    </html>
    """
    
    return self.send_simple_email(
        user.email,
        f"Appeal Submitted - Reference #{appeal.id} | GiverAI",
        html_body
    )

# Initialize email service
email_service = EmailService()

# Security middleware to check for suspended accounts (STANDALONE FUNCTION)
async def check_user_status(user: User):
    """Check if user account is in good standing"""
    if user.is_suspended:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account suspended: {user.suspension_reason}"
        )
    
    # Check if account is temporarily locked
    if user.account_locked_until and user.account_locked_until > datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account temporarily locked due to failed login attempts"
        )
    
    return user


def sanitize_input(text: str, max_length: int = 500) -> str:
    if not text:
        return ""
    text = text[:max_length].strip()
    text = bleach.clean(text, tags=[], strip=True)
    text = text.replace('\x00', '').replace('<', '&lt;').replace('>', '&gt;')
    return text

def sanitize_email(email: str) -> str:
    if not email:
        return ""
    email = email.strip().lower()
    email = bleach.clean(email, tags=[], strip=True)
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    if not email_pattern.match(email):
        raise ValueError("Invalid email format")
    return email

def sanitize_username(username: str) -> str:
    if not username:
        return ""
    username = username.strip().lower()
    username = re.sub(r'[^a-z0-9_]', '', username)
    if len(username) < 3:
        raise ValueError("Username must be at least 3 characters")
    return username[:30]

# Admin functions for account management (STANDALONE FUNCTIONS)
async def suspend_user(
    user_id: int, 
    reason: str, 
    suspended_by: str,
    db: Session
):
    reason = sanitize_input(reason)
    """Suspend a user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_suspended = True
    user.suspension_reason = reason
    user.suspended_at = datetime.utcnow()
    user.suspended_by = suspended_by
    
    db.commit()
    
    # Log this action
    print(f"User {user.username} suspended by {suspended_by}: {reason}")
    
    # Send email notification to user
    await email_service.send_suspension_email(user.email, reason)

async def force_password_reset(user_id: int, db: Session):
    """Force user to reset password on next login"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Invalidate all existing sessions by updating a security field
    user.last_password_change = datetime.utcnow()
    db.commit()
    
    # Create password reset record and send email
    reset_record = create_password_reset_record(user.id, db)
    await email_service.send_password_reset_email(user, reset_record.token)

async def lock_account_temporarily(user: User, db: Session, hours: int = 24):
    """Temporarily lock account (for failed login attempts)"""
    user.account_locked_until = datetime.utcnow() + timedelta(hours=hours)
    user.failed_login_attempts = 0  # Reset counter
    db.commit()

    try:
        await email_service.send_account_locked_email(user.email, hours)
    except Exception as e:
        print(f"Failed to send account locked email: {e}")

# Hacked account response workflow
async def handle_hacked_account_report(user_email: str, db: Session):
    """When user reports their account is hacked"""
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        return False

    # Immediate actions:
    # 1. Suspend account temporarily
    await suspend_user(
        user.id, 
        "Account security - reported as compromised", 
        "system_auto",
        db
    )
    
    # 2. Invalidate all sessions
    await force_password_reset(user.id, db)
    
    # 3. Log the incident
    incident_log = f"SECURITY INCIDENT - User {user.username} reported hacked at {datetime.utcnow()}"
    print(incident_log)  # In production, use proper logging
    
    # 4. Send instructions to user
    await email_service.send_account_recovery_email(user.email)
    
    return True

# Helper functions for password reset
def generate_reset_token():
    """Generate secure password reset token"""
    return secrets.token_urlsafe(32)

def create_password_reset_record(user_id: int, db: Session):
    """Create password reset record with hashed token storage"""
    # Invalidate any existing tokens for this user
    existing_tokens = db.query(PasswordReset).filter(
        PasswordReset.user_id == user_id,
        PasswordReset.used == False,
        PasswordReset.expires_at > datetime.utcnow()
    ).all()
    
    for token in existing_tokens:
        token.used = True
        token.used_at = datetime.utcnow()
    
    # Create new token
    raw_token = generate_reset_token() 
    hashed_token = hashlib.sha256(raw_token.encode()).hexdigest() 
    
    reset_record = PasswordReset(
        user_id=user_id,
        token=hashed_token,  # Store the hashed version
        expires_at=datetime.utcnow() + timedelta(hours=1)
    )
    
    db.add(reset_record)
    db.commit()
    
    # Add the raw token as a temporary attribute for email sending
    reset_record.raw_token = raw_token
    
    return reset_record

def validate_email_address(email: str) -> bool:
    try:
        validate_email(email)
        return True
    except EmailNotValidError:
        return False

# Add missing get_db dependency function
def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()
        
# Add this helper function for timezone conversion
def convert_to_eastern(utc_datetime):
    """Convert UTC datetime to Eastern Time"""
    if not utc_datetime:
        return None
    
    try:
        # Ensure it's timezone-aware UTC
        if utc_datetime.tzinfo is None:
            utc_datetime = pytz.utc.localize(utc_datetime)
        
        # Convert to Eastern Time
        eastern = pytz.timezone('US/Eastern')
        eastern_dt = utc_datetime.astimezone(eastern)
        
        return eastern_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
    except Exception as e:
        print(f"Timezone conversion error: {e}")
        return utc_datetime.strftime('%Y-%m-%d %H:%M:%S UTC') if utc_datetime else None
    
# Database models for email verification
class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)
    verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)  # Add this line
    user = relationship("User")

def check_admin_access(user):
    """Check if user has admin access"""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    if user.email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    return user

def is_admin_user(user):
    """Check if user has admin privileges (returns True/False)"""
    return user and user.email in ADMIN_EMAILS

def create_suspension_appeals_table():
    """Create the suspension_appeals table if it doesn't exist"""
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Suspension appeals table created/verified")
    except Exception as e:
        print(f"❌ Error creating suspension appeals table: {e}")

# Helper functions
def generate_verification_token():
    """Generate secure verification token"""
    return secrets.token_urlsafe(32)

def create_verification_record(user_id: int, db):
    """Create email verification record"""
    token = generate_verification_token()
    verification = EmailVerification(
        user_id=user_id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(hours=24)
    )
    db.add(verification)
    db.commit()
    return verification

def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies"""
    # Check for forwarded IP first (for reverse proxies)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, get the first one
        return forwarded_for.split(",")[0].strip()
    
    # Check other common proxy headers
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fallback to direct client IP
    return request.client.host if request.client else "Unknown"

def is_ip_banned(ip_address: str, db: Session) -> bool:
    """Check if an IP address is banned"""
    if not ip_address or ip_address == "Unknown":
        return False
    
    # Check for exact IP match
    ban = db.query(IPban).filter(
        IPban.ip_address == ip_address,
        IPban.is_active == True
    ).first()
    
    if ban:
        # Check if ban has expired
        if ban.expires_at and ban.expires_at < datetime.utcnow():
            ban.is_active = False
            db.commit()
            return False
        return True
    
    # TODO: Add subnet/range checking if needed
    return False

def ban_ip_address(ip_address: str, reason: str, banned_by: str, db: Session, expires_hours: int = None):
    """Ban an IP address"""
    if not ip_address or ip_address == "Unknown":
        return False
    
    # Check if IP is already banned
    existing_ban = db.query(IPban).filter(
        IPban.ip_address == ip_address,
        IPban.is_active == True
    ).first()
    
    if existing_ban:
        # Update existing ban
        existing_ban.reason = reason
        existing_ban.banned_by = banned_by
        existing_ban.banned_at = datetime.utcnow()
        if expires_hours:
            existing_ban.expires_at = datetime.utcnow() + timedelta(hours=expires_hours)
        else:
            existing_ban.expires_at = None  # Permanent
    else:
        # Create new ban
        expires_at = None
        if expires_hours:
            expires_at = datetime.utcnow() + timedelta(hours=expires_hours)
        
        ban = IPban(
            ip_address=ip_address,
            reason=reason,
            banned_by=banned_by,
            expires_at=expires_at
        )
        db.add(ban)
    
    db.commit()
    
    # Also mark any users with this IP as IP banned
    users_with_ip = db.query(User).filter(
        (User.last_known_ip == ip_address) | (User.registration_ip == ip_address)
    ).all()
    
    for user in users_with_ip:
        user.is_ip_banned = True
    
    db.commit()
    return True

def unban_ip_address(ip_address: str, db: Session):
    """Unban an IP address"""
    bans = db.query(IPban).filter(
        IPban.ip_address == ip_address,
        IPban.is_active == True
    ).all()
    
    for ban in bans:
        ban.is_active = False
    
    # Unmark users with this IP
    users_with_ip = db.query(User).filter(
        (User.last_known_ip == ip_address) | (User.registration_ip == ip_address)
    ).all()
    
    for user in users_with_ip:
        user.is_ip_banned = False
    
    db.commit()
    return True

# Middleware to check IP bans
async def check_ip_ban(request: Request, db: Session):
    """Check if the requesting IP is banned"""
    client_ip = get_client_ip(request)
    
    if is_ip_banned(client_ip, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: IP address is banned"
        )
    
def check_ip_ban(ip_address: str, db) -> bool:
    """Check if an IP address is banned"""
    ban = db.query(IPban).filter(  # Make sure this matches your model name
        IPban.ip_address == ip_address,
        IPban.is_active == True
    ).first()
    
    if ban:
        # Check if ban has expired
        if ban.expires_at and ban.expires_at < datetime.utcnow():
            ban.is_active = False
            db.commit()
            return False
        return True
    return False
# Add this helper function to safely handle user data in templates
def safe_user_data(user):
    """Safely return user data for templates, handling None values"""
    if not user:
        return {
            'username': 'Unknown',
            'email': 'Unknown',
            'plan': 'free',
            'suspended_at': None,
            'suspension_reason': None,
            'is_suspended': False
        }
    
    return {
        'username': getattr(user, 'username', 'Unknown'),
        'email': getattr(user, 'email', 'Unknown'),
        'plan': getattr(user, 'plan', 'free'),
        'suspended_at': getattr(user, 'suspended_at', None),
        'suspension_reason': getattr(user, 'suspension_reason', None),
        'is_suspended': getattr(user, 'is_suspended', False)
    }

    
def generate_email_change_token():
    """Generate secure email change token"""
    return secrets.token_urlsafe(32)

def create_email_change_request(user_id: int, new_email: str, db):
    """Create email change request record"""
    # Invalidate any existing email change requests for this user
    existing_requests = db.query(EmailChangeRequest).filter(
        EmailChangeRequest.user_id == user_id,
        EmailChangeRequest.verified == False,
        EmailChangeRequest.expires_at > datetime.utcnow()
    ).all()
    
    for request in existing_requests:
        request.expires_at = datetime.utcnow()  # Expire old requests
    
    # Create new request
    token = generate_email_change_token()
    change_request = EmailChangeRequest(
        user_id=user_id,
        new_email=new_email,
        token=token,
        expires_at=datetime.utcnow() + timedelta(hours=24)  # 24 hour expiry
    )
    db.add(change_request)
    db.commit()
    return change_request
    
class EmailChangeRequest(Base):
    __tablename__ = "email_change_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    new_email = Column(String, index=True)
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)
    verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)
    user = relationship("User")

class CsrfSettings(BaseModel):
    secret_key: str = os.getenv("SECRET_KEY")
    cookie_name: str = "fastapi-csrf-token"
    cookie_samesite: str = "lax"
    cookie_secure: bool = os.getenv("ENV") == "production" 
    cookie_httponly: bool = True
    header_name: str = "X-CSRF-Token"
    header_type: str = "form"
    max_age: int = 3600  # 1 hour 

# Load the config using decorator
@CsrfProtect.load_config
def get_csrf_config():
    return CsrfSettings()  

# ----- Auth Setup -----
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 2  # 1 day
ADMIN_EMAILS = set(email.strip() for email in os.getenv("ADMIN_EMAILS", "").split(",") if email.strip())

# Password hashing function with better debugging
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

def create_access_token(data: dict, expires_delta=None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user(db, username: str):
    try:
        # Defer loading password in non-auth routes
        return db.query(User).options(defer(User.hashed_password)).filter(User.username == username).first()
    except ProgrammingError as e:
        if "column users.is_admin does not exist" in str(e):
           raise HTTPException(
                status_code=500, 
                detail="Database schema out of date. Please contact support."
            )
        raise

def authenticate_user(db, username: str, password: str):
    # Load the user WITH password (don't use get_user which defers password)
    # Search by both username and email
    user = db.query(User).filter(
        (User.username == username) | (User.email == username)
    ).first()
    
    if not user:
        print(f"❌ No user found for: {username}")
        return None
    
    if not user.hashed_password:
        print(f"❌ No password hash for user: {user.username}")
        return None
    
    print(f"🔍 Verifying password for user: {user.username}")
    print(f"   Hash type: {type(user.hashed_password)}")
    print(f"   Hash length: {len(user.hashed_password)}")
    
    if not verify_password(password, user.hashed_password):
        print(f"❌ Password verification failed for user: {user.username}")
        return None
    
    print(f"✅ Password verified successfully for user: {user.username}")
    return user

def get_plan_features(plan_name):
    # Normalize plan names (handle monthly/yearly variants)
    base_plan = plan_name
    if plan_name in ["creator_monthly", "creator_yearly"]:
        base_plan = "creator"
    elif plan_name in ["small_team_monthly", "small_team_yearly"]:
        base_plan = "small_team"

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
    
    db = SessionLocal()
    token = request.cookies.get("access_token")
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
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
    
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    
    return user

async def get_current_user_or_none(request: Request) -> Optional[User]:
    """Get current user without raising exception if not authenticated"""
    db = SessionLocal()
    try:
        token = request.cookies.get("access_token")
        if not token:
            return None
        
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        
        user = db.query(User).filter(User.username == username).first()
        return user
    except (JWTError, Exception):
        return None
    
def get_optional_user(request: Request, allow_suspended: bool = False):
    """Get optional user (returns None if not authenticated)"""
    try:
        token = request.cookies.get("access_token")
        if not token:
            return None
            
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
            
        db = SessionLocal()
        try:
            user = get_user(db, username)
            if user is None:
                return None
            
            # Only apply security checks if not allowing suspended users
            if not allow_suspended:
                if user.is_suspended:
                    return None
                if user.account_locked_until and user.account_locked_until > datetime.utcnow():
                    return None
            
            user.features = get_plan_features(user.plan)
            return user
        finally:
            db.close()
            
    except JWTError:
        return None
    except Exception as e:
        print(f"Error in get_optional_user: {e}")
        return None
   
def get_suspended_user(request: Request):
    """Special function to get user for suspended routes - allows suspended users"""
    return get_current_user(request, allow_suspended=True)

def get_optional_suspended_user(request: Request):
    """Special function to get optional user for suspended routes - allows suspended users"""
    return get_optional_user(request, allow_suspended=True)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def apply_plan_features(user):
    if not hasattr(user, 'features'):
        user.features = get_plan_features(user.plan)
    return user
    
# ---- FastAPI Setup -----
app = FastAPI()

# Add middlewares
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["giverai.me", "www.giverai.me", "localhost"])

# Exception handler
@app.exception_handler(CsrfProtectError)
def csrf_protect_exception_handler(request: Request, exc: CsrfProtectError):
    return JSONResponse(
        status_code=exc.status_code, 
        content={"detail": "CSRF token validation failed"}
    )

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."}
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        # Redirect to login for authentication errors
        return RedirectResponse(
            url=f"/login?error=Please+log+in+first&redirect={request.url.path}",
            status_code=303
        )
    # Let other HTTP exceptions pass through
    raise exc

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    TrustedHostMiddleware, 
    allowed_hosts=["giverai.me", "www.giverai.me"]
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["100/hour"])
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc: HTTPException):
    """Custom 404 page handler"""
    user = get_optional_user(request)
    return templates.TemplateResponse("404.html", {
        "request": request,
        "user": user
    }, status_code=404)

def migrate_database():
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
    # Check and add all missing columns to users table
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
            'posting_frequency': "ALTER TABLE users ADD COLUMN posting_frequency VARCHAR",
            'original_plan': "ALTER TABLE users ADD COLUMN original_plan VARCHAR",
            'last_login': "ALTER TABLE users ADD COLUMN last_login TIMESTAMP",
            'suspended_at': "ALTER TABLE users ADD COLUMN suspended_at TIMESTAMP",
            'suspended_by': "ALTER TABLE users ADD COLUMN suspended_by VARCHAR",
            'suspension_reason': "ALTER TABLE users ADD COLUMN suspension_reason TEXT",
            'last_known_ip': "ALTER TABLE users ADD COLUMN last_known_ip VARCHAR",
            'registration_ip': "ALTER TABLE users ADD COLUMN registration_ip VARCHAR",
            'is_ip_banned': "ALTER TABLE users ADD COLUMN is_ip_banned BOOLEAN DEFAULT FALSE"
        }
        
        for col_name, sql in required_columns.items():
            if col_name not in columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    print(f"Added {col_name} column to users table")
                except Exception as e:
                    print(f"Error adding {col_name}: {e}")

    # Get list of existing tables
    existing_tables = inspector.get_table_names()
    
    # Create missing tables
    tables_to_create = []
    
    if 'generated_tweets' not in existing_tables:
        tables_to_create.append('generated_tweets')
    
    if 'team_members' not in existing_tables:
        tables_to_create.append('team_members')
        
    if 'email_verifications' not in existing_tables:
        tables_to_create.append('email_verifications')
    
    if 'usage' not in existing_tables:
        tables_to_create.append('usage')

    if 'email_change_requests' not in existing_tables:
        tables_to_create.append('email_change_requests')
    
    if 'ip_bans' not in existing_tables:
        tables_to_create.append('ip_bans')

    if 'password_resets' not in existing_tables:
        print("Creating password_resets table...")
        try:
            Base.user_metadata.tables['password_resets'].create(bind=engine)
            print("✅ password_resets table created")
        except Exception as e:
            print(f"❌ Error creating password_resets table: {e}")
    
    # Create the missing tables
    if tables_to_create:
        print(f"Creating missing tables: {tables_to_create}")
        try:
            # Create all tables defined in Base.user_metadata
            Base.user_metadata.create_all(bind=engine)
            print("All missing tables created successfully")
        except Exception as e:
            print(f"Error creating tables: {e}")
            # Try creating them individually
            for table_name in tables_to_create:
                try:
                    if table_name in Base.user_metadata.tables:
                        Base.user_metadata.tables[table_name].create(bind=engine)
                        print(f"Created {table_name} table")
                except Exception as table_error:
                    print(f"Error creating {table_name}: {table_error}")
    
    print("Database migration completed")
    
def update_user_model():
    """Add missing columns to users table"""
    engine = create_engine(DATABASE_URL)
    
    missing_columns = {
        'last_login': "ALTER TABLE users ADD COLUMN last_login TIMESTAMP",
        'is_suspended': "ALTER TABLE users ADD COLUMN is_suspended BOOLEAN DEFAULT FALSE",
        'suspension_reason': "ALTER TABLE users ADD COLUMN suspension_reason TEXT",
        'suspended_at': "ALTER TABLE users ADD COLUMN suspended_at TIMESTAMP",
        'suspended_by': "ALTER TABLE users ADD COLUMN suspended_by VARCHAR",
        'failed_login_attempts': "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0",
        'last_failed_login': "ALTER TABLE users ADD COLUMN last_failed_login TIMESTAMP",
        'account_locked_until': "ALTER TABLE users ADD COLUMN account_locked_until TIMESTAMP"
    }
    
    inspector = inspect(engine)
    if 'users' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('users')]
        
        for col_name, sql in missing_columns.items():
            if col_name not in columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    print(f"Added {col_name} column to users table")
                except Exception as e:
                    print(f"Error adding {col_name}: {e}")

# Helper function to create dependency for database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def migrate_database_suspension():
    """Add suspension-related database updates"""
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
    # Get list of existing tables
    existing_tables = inspector.get_table_names()
    
    # Create new tables if they don't exist
    if 'ip_bans' not in existing_tables:
        print("Creating ip_bans table...")
        try:
            Base.metadata.tables['ip_bans'].create(bind=engine)
            print("✅ ip_bans table created")
        except Exception as e:
            print(f"❌ Error creating ip_bans table: {e}")
    
    if 'suspension_appeals' not in existing_tables:
        print("Creating suspension_appeals table...")
        try:
            Base.metadata.tables['suspension_appeals'].create(bind=engine)
            print("✅ suspension_appeals table created")
        except Exception as e:
            print(f"❌ Error creating suspension_appeals table: {e}")
    
    # Add new columns to users table
    if 'users' in existing_tables:
        columns = [col['name'] for col in inspector.get_columns('users')]
        
        new_columns = {
            'last_known_ip': "ALTER TABLE users ADD COLUMN last_known_ip VARCHAR",
            'registration_ip': "ALTER TABLE users ADD COLUMN registration_ip VARCHAR", 
            'is_ip_banned': "ALTER TABLE users ADD COLUMN is_ip_banned BOOLEAN DEFAULT FALSE"
        }
        
        for col_name, sql in new_columns.items():
            if col_name not in columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    print(f"Added {col_name} column to users table")
                except Exception as e:
                    print(f"Error adding {col_name}: {e}")

    print("Suspension database migration completed")

def update_user_table_for_suspension():
    """Add missing suspension-related columns to users table"""
    engine = create_engine(DATABASE_URL)
    
    try:
        with engine.begin() as conn:
            # Check existing columns
            inspector = inspect(engine)
            if 'users' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('users')]
                
                # Add missing suspension columns
                suspension_columns = {
                    'is_suspended': "ALTER TABLE users ADD COLUMN is_suspended BOOLEAN DEFAULT FALSE",
                    'suspension_reason': "ALTER TABLE users ADD COLUMN suspension_reason TEXT",
                    'suspended_at': "ALTER TABLE users ADD COLUMN suspended_at TIMESTAMP",
                    'suspended_by': "ALTER TABLE users ADD COLUMN suspended_by VARCHAR",
                    'last_login': "ALTER TABLE users ADD COLUMN last_login TIMESTAMP"
                }
                
                for col_name, sql in suspension_columns.items():
                    if col_name not in columns:
                        try:
                            conn.execute(text(sql))
                            print(f"✅ Added {col_name} column to users table")
                        except Exception as e:
                            print(f"❌ Error adding {col_name}: {e}")
                
                # Make sure suspension_reason is TEXT type (not VARCHAR)
                try:
                    conn.execute(text("ALTER TABLE users ALTER COLUMN suspension_reason TYPE TEXT"))
                    print("✅ Updated suspension_reason to TEXT type")
                except Exception as e:
                    # This might fail if it's already TEXT, which is fine
                    pass
                    
        print("✅ User table suspension fields updated")
    except Exception as e:
        print(f"❌ Error updating user table: {e}")

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
        
# Activity logging function
def log_user_activity(
    user_id: int, 
    activity_type: str, 
    description: str = None, 
    ip_address: str = None,
    user_agent: str = None,
    user_metadata: dict = None,
    db: Session = None
):
    """Log user activity"""
    if db is None:
        db = SessionLocal()
        should_close = True
    else:
        should_close = False
    
    try:
        activity = UserActivity(
            user_id=user_id,
            activity_type=activity_type,
            description=description,
            ip_address=ip_address,
            user_agent=user_agent,
            user_metadata=json.dumps(user_metadata) if user_metadata else None
        )
        db.add(activity)
        if should_close:
            db.commit()
    except Exception as e:
        print(f"Error logging activity: {e}")
        if should_close:
            db.rollback()
    finally:
        if should_close:
            db.close()

# ---- ROUTES ----

@app.get("/")
def root_redirect(request: Request):
    access_token = request.cookies.get("access_token")
    if access_token:
        try:
            payload = jwt.decode(access_token, SECRET_KEY, algorithms=["HS256"])
            return RedirectResponse(url="/dashboard")
        except:
            pass
    return RedirectResponse(url="/home")

@app.get("/home")
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "user": None})
    
@app.head("/")  

def index(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "user": user
    })
def verify_recaptcha(recaptcha_response):
    """Verify reCAPTCHA response with Google"""
    secret_key = os.getenv("RECAPTCHA_SECRET_KEY")
    if not secret_key:
        print("❌ Missing RECAPTCHA_SECRET_KEY")
        return False
    
    data = {
        'secret': secret_key,
        'response': recaptcha_response
    }
    
    try:
        response = requests.post('https://www.google.com/recaptcha/api/siteverify', data=data)
        result = response.json()
        return result.get('success', False)
    except Exception as e:
        print(f"❌ reCAPTCHA verification error: {str(e)}")
        return False    

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon.ico"""
    favicon_path = os.path.join("static", "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    else:
        # Return 404 if favicon doesn't exist
        raise HTTPException(status_code=404, detail="Favicon not found")

@limiter.limit("3/minute") 
@app.get("/register", response_class=HTMLResponse)
def register(request: Request, csrf_protect: CsrfProtect = Depends(), success: str = None):
    user = get_optional_user(request)
    csrf_response = csrf_protect.generate_csrf()
    
    # Extract token from tuple
    csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
    
    success_message = None
    if success == "registration_complete":
        success_message = "Registration successful! Please check your email to verify your account."
    
    response = templates.TemplateResponse("register.html", {
        "request": request, 
        "user": user,
        "success": success_message,
        "csrf_token": csrf_token,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })
    
    # SET THE COOKIE
    response.set_cookie(
        key="fastapi-csrf-token",
        value=csrf_token,
        max_age=3600,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax"
    )
    
    return response

@limiter.limit("3/hour")
@app.post("/register", response_class=HTMLResponse)
async def register_post(
    request: Request, 
    username: str = Form(...), 
    email: str = Form(...), 
    password: str = Form(...),
    csrf_token: str = Form(...),
    csrf_protect: CsrfProtect = Depends(),  # Add CSRF protection
    g_recaptcha_response: str = Form(alias="g-recaptcha-response", default="")
):
    # Sanitize inputs
    username = sanitize_input(username)
    email = sanitize_input(email)
    
    # Manual CSRF validation
    cookie_token = request.cookies.get("fastapi-csrf-token")
    
    if not cookie_token or cookie_token != csrf_token:
        csrf_response = csrf_protect.generate_csrf()
        new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
        
        response = templates.TemplateResponse("register.html", {
            "request": request,
            "user": None,
            "error": "Invalid CSRF token. Please refresh and try again.",
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": new_csrf_token
        })
        response.set_cookie(
            key="fastapi-csrf-token",
            value=new_csrf_token,
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        return response
    
    db = SessionLocal()
    try:
        print(f"Starting registration for username: {username}")
        
        # Get the real client IP
        client_ip = get_real_client_ip(request)
        print(f"🌐 Client IP for registration: {client_ip}")
        
        # Verify reCAPTCHA first
        if not verify_recaptcha(g_recaptcha_response):
            print("❌ reCAPTCHA verification failed")
            csrf_response = csrf_protect.generate_csrf()
            new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
            
            response = templates.TemplateResponse("register.html", {
                "request": request, 
                "user": None,
                "error": "Please complete the reCAPTCHA verification",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
                "csrf_token": new_csrf_token
            })
            response.set_cookie(
                key="fastapi-csrf-token",
                value=new_csrf_token,
                max_age=3600,
                path="/",
                secure=True,
                httponly=True,
                samesite="lax"
            )
            return response
        
        print("✅ reCAPTCHA verified successfully")
        
        # Check username exists
        existing_username = db.query(User).filter(User.username == username).first()
        if existing_username:
            print(f"Username {username} already exists")
            csrf_response = csrf_protect.generate_csrf()
            new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
            
            response = templates.TemplateResponse("register.html", {
                "request": request, 
                "user": None,
                "error": "Username already exists",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
                "csrf_token": new_csrf_token
            })
            response.set_cookie(
                key="fastapi-csrf-token",
                value=new_csrf_token,
                max_age=3600,
                path="/",
                secure=True,
                httponly=True,
                samesite="lax"
            )
            return response
        
        # Check email exists
        existing_email = db.query(User).filter(User.email == email).first()
        if existing_email:
            print(f"Email {email} already registered")
            csrf_response = csrf_protect.generate_csrf()
            new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
            
            response = templates.TemplateResponse("register.html", {
                "request": request, 
                "user": None,
                "error": "Email already registered",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
                "csrf_token": new_csrf_token
            })
            response.set_cookie(
                key="fastapi-csrf-token",
                value=new_csrf_token,
                max_age=3600,
                path="/",
                secure=True,
                httponly=True,
                samesite="lax"
            )
            return response
        
        # Hash password and create user
        hashed_password = hash_password(password)
        
        new_user = User(
            username=username,
            email=email,
            hashed_password=hashed_password,
            plan="free",
            is_active=False,
            created_at=datetime.utcnow(),
            registration_ip=client_ip
        )
        
        db.add(new_user)
        db.flush()
        user_id = new_user.id
        db.commit()
        
        # Create verification and send emails
        verification = create_verification_record(user_id, db)
        
        try:
            await email_service.send_verification_email(new_user, verification.token)
            print("✅ Verification email sent successfully")
        except Exception as e:
            print(f"❌ Failed to send verification email: {str(e)}")
        
        try:
            await email_service.send_welcome_email(new_user)
            print("✅ Welcome email sent successfully")
        except Exception as e:
            print(f"❌ Failed to send welcome email: {str(e)}")
        
        print(f"User registered successfully with ID: {user_id}")
        
        return templates.TemplateResponse("register_success.html", {
            "request": request,
            "user": None,
            "username": username,
            "email": email
        })
        
    except Exception as e:
        db.rollback()
        print(f"Registration error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        csrf_response = csrf_protect.generate_csrf()
        new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
        
        response = templates.TemplateResponse("register.html", {
            "request": request,
            "user": None,
            "error": "Registration failed. Please try again.",
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": new_csrf_token
        })
        response.set_cookie(
            key="fastapi-csrf-token",
            value=new_csrf_token,
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        return response
    finally:
        db.close()

import uuid
@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://www.google.com https://www.gstatic.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' https:; connect-src 'self'; frame-src https://www.google.com;"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

@app.middleware("http")
async def logo_cache_middleware(request: Request, call_next):
    """Add cache headers for logo and static assets"""
    response = await call_next(request)
    
    # Add cache headers for static assets
    if request.url.path.startswith("/static/"):
        if request.url.path.endswith((".png", ".jpg", ".jpeg", ".ico", ".svg")):
            # Cache images for 7 days
            response.headers["Cache-Control"] = "public, max-age=604800"
        elif request.url.path.endswith((".css", ".js")):
            # Cache CSS/JS for 1 day
            response.headers["Cache-Control"] = "public, max-age=86400"
    
    return response

@app.get("/_health")
def health_check():
    try:
        # Test database connection
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
    
@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request, csrf_protect: CsrfProtect = Depends()):
    """Display the forgot password form"""
    user = get_optional_user(request)
    csrf_response = csrf_protect.generate_csrf()
    
    # Extract just the token from the tuple
    if isinstance(csrf_response, tuple):
        csrf_token = csrf_response[0]  
    else:
        csrf_token = csrf_response
    
    response = templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
        "user": user,
        "csrf_token": csrf_token
    })

    response.set_cookie(
        key="fastapi-csrf-token",
        value=csrf_token,
        max_age=3600,
        path="/",
        secure=IS_PRODUCTION,  
        httponly=True,
        samesite="lax"
    )
    
    return response

@limiter.limit("3/hour")
@app.post("/forgot-password")
async def forgot_password_post(
    request: Request,
    reset_type: str = Form(...),
    email_or_username: str = Form(...),
    csrf_token: str = Form(...),  
    g_recaptcha_response: str = Form(alias="g-recaptcha-response", default=""),
    csrf_protect: CsrfProtect = Depends()
):
    client_ip = get_real_client_ip(request)
    
    # Manual CSRF validation
    cookie_token = request.cookies.get("fastapi-csrf-token")
    
    if not cookie_token or cookie_token != csrf_token:
        csrf_response = csrf_protect.generate_csrf()
        new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
        
        response = templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "Invalid CSRF token. Please refresh and try again.",
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": new_csrf_token
        })
        response.set_cookie(
            key="fastapi-csrf-token",
            value=new_csrf_token,
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        return response
    
    # CSRF is valid, continue with rest of the logic
    if not verify_recaptcha(g_recaptcha_response):
        csrf_response = csrf_protect.generate_csrf()
        new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
        
        response = templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "Please complete the reCAPTCHA verification",
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": new_csrf_token
        })
        response.set_cookie(
            key="fastapi-csrf-token",
            value=new_csrf_token,
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        return response
    
    email_or_username = sanitize_input(email_or_username)
    db = SessionLocal()
    
    try:
        user = db.query(User).filter(
            (User.email == email_or_username) | (User.username == email_or_username)
        ).first()
        
        csrf_response = csrf_protect.generate_csrf()
        new_csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
        
        if not user:
            response = templates.TemplateResponse("forgot_password.html", {
                "request": request,
                "user": None,
                "success": "If an account with that email/username exists, we've sent you an email.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
                "csrf_token": new_csrf_token
            })
            response.set_cookie(
                key="fastapi-csrf-token",
                value=new_csrf_token,
                max_age=3600,
                path="/",
                secure=True,
                httponly=True,
                samesite="lax"
            )
            return response
        
        if reset_type == "password":
            reset_record = create_password_reset_record(user.id, db)
            try:
                await email_service.send_password_reset_email(user, reset_record.raw_token, client_ip)
                success_message = "Password reset email sent! Check your inbox."
            except Exception as e:
                print(f"Failed to send password reset email: {str(e)}")
                success_message = "If an account exists, we've sent a reset email."
        
        elif reset_type == "username":
            try:
                await email_service.send_username_reminder_email(user)
                success_message = "Username reminder sent! Check your inbox."
            except Exception as e:
                print(f"Failed to send username reminder: {str(e)}")
                success_message = "If an account exists, we've sent a username reminder."
        
        response = templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "user": None,
            "success": success_message,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": new_csrf_token
        })
        response.set_cookie(
            key="fastapi-csrf-token",
            value=new_csrf_token,
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        return response
    
    finally:
        db.close()
        
@app.get("/resend-verification", response_class=HTMLResponse)
def resend_verification_get(request: Request):
    """Display the resend verification form"""
    user = get_optional_user(request)
    return templates.TemplateResponse("resend_verification.html", {
        "request": request,
        "user": user,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })

@limiter.limit("30/hour")
@app.post("/resend-verification")
async def resend_verification_post(
    request: Request,
    email_or_username: str = Form(...),
    g_recaptcha_response: str = Form(alias="g-recaptcha-response", default=""),
    csrf_protect: CsrfProtect = Depends()
):
    """Resend verification email"""
    db = SessionLocal()
    try:
        # Verify reCAPTCHA
        if not verify_recaptcha(g_recaptcha_response):
            csrf_token = csrf_protect.generate_csrf() 
            return templates.TemplateResponse("resend_verification.html", {
                "request": request,
                "user": None,
                "error": "Please complete the reCAPTCHA verification",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
                "email_or_username": email_or_username,
                "csrf_token": csrf_token
            })

        # Find user by email or username
        user = db.query(User).filter(
            (User.email == email_or_username) | (User.username == email_or_username)
        ).first()

        if not user:
            csrf_token = csrf_protect.generate_csrf() 
            return templates.TemplateResponse("resend_verification.html", {
                "request": request,
                "user": None,
                "success": "If an account with that email/username exists and needs verification, we've sent a new verification email.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })

        # Check if user is already verified
        if user.is_active:
            return templates.TemplateResponse("resend_verification.html", {
                "request": request,
                "user": None,
                "info": "Your account is already verified! You can log in now.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })

        # Check for rate limiting (max 3 verification emails per hour)
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        recent_verifications = db.query(EmailVerification).filter(
            EmailVerification.user_id == user.id,
            EmailVerification.created_at > one_hour_ago
        ).count()

        if recent_verifications >= 3:
            return templates.TemplateResponse("resend_verification.html", {
                "request": request,
                "user": None,
                "error": "Too many verification attempts. Please wait an hour before requesting another verification email.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })

        # Invalidate old verification tokens for this user
        old_verifications = db.query(EmailVerification).filter(
            EmailVerification.user_id == user.id,
            EmailVerification.verified == False
        ).all()
        
        for old_verification in old_verifications:
            old_verification.expires_at = datetime.utcnow()  # Expire old tokens
        
        # Create new verification record
        verification = create_verification_record(user.id, db)

        # Send verification email
        try:
            await email_service.send_verification_email(user, verification.token)
            success_message = f"Verification email sent to {user.email}! Please check your inbox and spam folder."
            print(f"✅ Resent verification email to {user.email}")
        except Exception as e:
            print(f"❌ Failed to send verification email: {str(e)}")
            success_message = "If an account exists and needs verification, we've sent a new verification email."

        csrf_token = csrf_protect.generate_csrf() 
        return templates.TemplateResponse("resend_verification.html", {
            "request": request,
            "user": None,
            "success": success_message,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY"),
            "csrf_token": csrf_token
        })

    finally:
        db.close()

@limiter.limit("30/hour")
# Also add a quick resend route for already logged-in users who aren't verified
@app.post("/quick-resend-verification")
async def quick_resend_verification(request: Request):
    """Quick resend for logged-in users (if they somehow bypassed verification)"""
    try:
        # Try to get current user, but don't fail if not authenticated
        token = request.cookies.get("access_token")
        if not token:
            return JSONResponse({"success": False, "error": "Not authenticated"})
        
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == username).first()
            
            if not user:
                return JSONResponse({"success": False, "error": "User not found"})
            
            if user.is_active:
                return JSONResponse({"success": False, "error": "Already verified"})
            
            # Create new verification
            verification = create_verification_record(user.id, db)
            
            # Send email
            await email_service.send_verification_email(user, verification.token)
            
            return JSONResponse({
                "success": True, 
                "message": f"Verification email sent to {user.email}"
            })
            
        finally:
            db.close()
            
    except Exception as e:
        print(f"Quick resend error: {str(e)}")
        return JSONResponse({"success": False, "error": "Failed to resend verification"})
        
# Reset Password Form
@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_get(request: Request, token: str = Query(None)):
    if not token:
        return templates.TemplateResponse("reset_password_error.html", {
            "request": request,
            "error": "Invalid or missing reset token. Please request a new password reset."
        })
    db = SessionLocal()
    try:
        # Hash the provided token to compare with stored hash
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        
        # Verify token using hashed version
        reset_record = db.query(PasswordReset).filter(
            PasswordReset.token == hashed_token,  # Compare with hashed version
            PasswordReset.used == False,
            PasswordReset.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_record:
            return templates.TemplateResponse("reset_password_error.html", {
                "request": request,
                "error": "Invalid or expired reset token"
            })
        
        return templates.TemplateResponse("reset_password.html", {
            "request": request,
            "token": token,  # Pass the original token back to the form
            "username": reset_record.user.username
        })
    finally:
        db.close()

@limiter.limit("30/hour")
@app.post("/reset-password")
def reset_password_post(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    db = SessionLocal()
    try:
        # Get IP address for logging
        ip_address = get_real_client_ip(request)
        
        # Validate passwords match
        if new_password != confirm_password:
            return templates.TemplateResponse("reset_password.html", {
                "request": request,
                "token": token,
                "error": "Passwords don't match"
            })
        
        # Validate password strength
        if len(new_password) < 8:
            return templates.TemplateResponse("reset_password.html", {
                "request": request,
                "token": token,
                "error": "Password must be at least 8 characters long"
            })
        
        # Hash the provided token to find the record
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        
        # Verify and use token
        reset_record = db.query(PasswordReset).filter(
            PasswordReset.token == hashed_token,  # Compare with hashed version
            PasswordReset.used == False,
            PasswordReset.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_record:
            return templates.TemplateResponse("reset_password_error.html", {
                "request": request,
                "error": "Invalid or expired reset token"
            })
        
        # Update user's password
        user = reset_record.user
        user.hashed_password = hash_password(new_password)
        
        # Mark token as used
        reset_record.used = True
        reset_record.used_at = datetime.utcnow()
        
        db.commit()
        
        # Send confirmation email
        try:
            email_service.send_password_reset_success_email(user, ip_address)
        except Exception as e:
            print(f"Failed to send password reset confirmation: {str(e)}")
        
        return templates.TemplateResponse("reset_success.html", {
            "request": request,
            "username": user.username
        })
        
    finally:
        db.close()
        
# Custom exception handler for 404 errors
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors with custom page"""
    return templates.TemplateResponse(
        "404.html", 
        {"request": request}, 
        status_code=404
    )

# Handle StarletteHTTPException (includes 404s that aren't caught above)
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions"""
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "404.html", 
            {"request": request}, 
            status_code=404
        )
    elif exc.status_code == 500:
        return templates.TemplateResponse(
            "500.html", 
            {"request": request}, 
            status_code=500
        )
    elif exc.status_code == 403:
        return templates.TemplateResponse(
            "403.html", 
            {
                "request": request,
                "error_message": str(exc.detail) if hasattr(exc, 'detail') else "Forbidden"
            }, 
            status_code=403
        )
    elif exc.status_code == 423:
        return templates.TemplateResponse(
            "423.html", 
            {
                "request": request,
                "error_message": str(exc.detail) if hasattr(exc, 'detail') else "Account Locked"
            }, 
            status_code=423
        )
    else:
        # For other HTTP errors, return a generic error page
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "error_message": str(exc.detail) if hasattr(exc, 'detail') else "An error occurred"
            },
            status_code=exc.status_code
        )

# Handle validation errors (422)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle form validation errors"""
    return templates.TemplateResponse(
        "400.html",
        {
            "request": request,
            "error_details": exc.errors()
        },
        status_code=422
    )

# Handle FastAPI HTTPException
@app.exception_handler(HTTPException)
async def fastapi_http_exception_handler(request: Request, exc: HTTPException):
    """Handle FastAPI HTTP exceptions"""
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "404.html", 
            {"request": request}, 
            status_code=404
        )
    elif exc.status_code == 403:
        return templates.TemplateResponse(
            "403.html", 
            {
                "request": request,
                "error_message": str(exc.detail)
            }, 
            status_code=403
        )
    elif exc.status_code == 423:
        return templates.TemplateResponse(
            "423.html", 
            {
                "request": request,
                "error_message": str(exc.detail)
            }, 
            status_code=423
        )
    else:
        # Re-raise for other status codes to be handled elsewhere
        raise exc
    
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Handle specific ValueError exceptions"""
    print(f"ValueError: {str(exc)}")
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": 400,
            "error_message": "Invalid input provided"
        },
        status_code=400
    )
 
@app.get("/verify-email")
def verify_email(request: Request, token: str = Query(...)):
    db = SessionLocal()
    try:
        verification = db.query(EmailVerification).filter(
            EmailVerification.token == token,
            EmailVerification.expires_at > datetime.utcnow()
        ).first()
        
        if not verification:
            return templates.TemplateResponse("verification_failed.html", {
                "request": request,
                "error": "Invalid or expired verification token"
            })
        
        # Mark email as verified
        verification.verified = True
        verification.verified_at = datetime.utcnow()  # Set verification time
        db.commit()
        
        # Update user status
        user = verification.user
        user.is_active = True
        db.commit()
        
        return templates.TemplateResponse("verification_success.html", {
            "request": request
        })
    finally:
        db.close()
        
@app.get("/verify-email-change")
def verify_email_change(request: Request, token: str = Query(...)):
    db = SessionLocal()
    try:
        # Find the email change request
        change_request = db.query(EmailChangeRequest).filter(
            EmailChangeRequest.token == token,
            EmailChangeRequest.verified == False,
            EmailChangeRequest.expires_at > datetime.utcnow()
        ).first()
        
        if not change_request:
            return templates.TemplateResponse("email_change_failed.html", {
                "request": request,
                "error": "Invalid or expired email change verification link"
            })
        
        # Get the user
        user = change_request.user
        old_email = user.email
        new_email = change_request.new_email
        
        # Get IP address
        ip_address = get_real_client_ip(request) if request.client else "Unknown"
        
        # Update user's email
        user.email = new_email
        
        # Mark the change request as verified
        change_request.verified = True
        change_request.verified_at = datetime.utcnow()
        
        db.commit()
        
        # Send notification to OLD email address
        try:
            email_service.send_email_changed_notification(
                user, old_email, new_email, ip_address
            )
            print("✅ Email change notification sent to old address")
        except Exception as e:
            print(f"❌ Failed to send email change notification: {str(e)}")
        
        return templates.TemplateResponse("email_change_success.html", {
            "request": request,
            "old_email": old_email,
            "new_email": new_email,
            "username": user.username
        })
        
    finally:
        db.close()

ADMIN_EMAILS_ENV = os.getenv("ADMIN_EMAILS", "")
if not ADMIN_EMAILS_ENV:
    raise ValueError("ADMIN_EMAILS environment variable must be set")
ADMIN_USERS = set(email.strip() for email in ADMIN_EMAILS_ENV.split(",") if email.strip())

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def is_admin_user(user) -> bool:
    """Check if a user object is an admin"""
    if not user:
        return False
    return user.email in ADMIN_USERS

def get_admin_user(request: Request):
    """Get current user and verify admin status - returns 404 if not admin"""
    try:
        user = get_current_user(request)
    except HTTPException:
        # User not logged in
        raise HTTPException(status_code=404, detail="Page not found")
    
    # Check if user is admin
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Page not found")
    
    return user

def get_regular_user(request: Request):
    """Get current user for regular dashboard - NOT for admin routes"""
    return get_current_user(request)

@app.middleware("http")
async def ip_ban_middleware(request: Request, call_next):
    """Middleware to check for IP bans"""
    # Skip IP ban check for admin routes (so admins can unban themselves)
    if request.url.path.startswith("/admin"):
        response = await call_next(request)
        return response
    
    # Skip for static files and health checks
    skip_routes = ["/static", "/favicon.ico", "/_health"]
    if any(request.url.path.startswith(route) for route in skip_routes):
        response = await call_next(request)
        return response
    
    # Get client IP
    ip_address = get_real_client_ip(request) if request.client else None
    
    if ip_address:
        db = SessionLocal()
        try:
            if is_ip_banned(ip_address, db):
                # Use the styled template instead of inline HTML
                return templates.TemplateResponse("ip_banned.html", {
                    "request": request,
                    "ip_address": ip_address
                }, status_code=403)
        finally:
            db.close()
    
    response = await call_next(request)
    return response

# Middleware to track user IPs and check bans
@app.middleware("http") 
async def ip_tracking_middleware(request: Request, call_next):
    """Track user IPs and check for IP bans"""
    client_ip = get_client_ip(request)
    
    # Skip IP checking for certain routes
    skip_routes = ["/static", "/favicon.ico", "/_health"]
    if any(request.url.path.startswith(route) for route in skip_routes):
        return await call_next(request)
    
    # Check IP ban for all routes except suspended page and admin
    if not request.url.path.startswith(("/suspended", "/admin")):
        db = SessionLocal()
        try:
            if is_ip_banned(client_ip, db):
                # Use styled template instead of basic HTML
                return templates.TemplateResponse("ip_banned.html", {
                    "request": request,
                    "ip_address": client_ip
                }, status_code=403)
        finally:
            db.close()
    
    response = await call_next(request)
    
    # Track IP for logged-in users
    if hasattr(request.state, 'user'):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == request.state.user.id).first()
            if user:
                user.last_known_ip = client_ip
                db.commit()
        except Exception as e:
            print(f"Error tracking IP: {e}")
        finally:
            db.close()
    
    return response

@app.get("/admin")
async def admin_dashboard(
    request: Request,
    user: User = Depends(get_admin_user),
    success: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db)
):
    """Admin dashboard page"""
    try:
        if not user or not is_admin_user(user):
            raise HTTPException(status_code=404, detail="Page not found")
        
        last_login_eastern = convert_to_eastern(user.last_login) if user.last_login else None
        
        # Get statistics
        total_users = db.query(User).count()
        suspended_count = db.query(User).filter(User.is_suspended == True).count()
        
        # Recent signups (last 7 days)
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        recent_signups = db.query(User).filter(User.created_at >= seven_days_ago).count()
        
        # Active today (logged in last 24 hours)
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        active_today = db.query(User).filter(User.last_login >= one_day_ago).count()
        
        return templates.TemplateResponse("admin/dashboard.html", {
            "request": request,
            "user": user,
            "total_users": total_users,
            "last_login_eastern": last_login_eastern,
            "suspended_count": suspended_count,
            "recent_signups": recent_signups,
            "active_today": active_today,
            "success": success,
            "error": error
        })
        
    except HTTPException:
        # Re-raise HTTPException (404)
        raise
        
    except Exception as e:
        print(f"Admin dashboard error: {str(e)}")
        raise HTTPException(status_code=404, detail="Page not found")
        
@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard_updated(
    request: Request,
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """Enhanced admin dashboard with suspension appeals and IP bans"""
    
    # Get user statistics
    total_users = db.query(User).count()
    suspended_count = db.query(User).filter(User.is_suspended == True).count()
    recent_signups = db.query(User).filter(
        User.created_at > datetime.utcnow() - timedelta(days=7)
    ).count()
    active_today = db.query(User).filter(
        User.last_login > datetime.utcnow() - timedelta(hours=24)
    ).count()
    
    # Get pending appeals
    pending_appeals = db.query(SuspensionAppeal).filter(
        SuspensionAppeal.status == "pending"
    ).count()
    
    # Get active IP bans
    active_ip_bans = db.query(IPban).filter(IPban.is_active == True).count()
    last_login_eastern = convert_to_eastern(User.last_login) if User.last_login else None
    
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "user": admin_user,
        "total_users": total_users,
        "suspended_count": suspended_count,
        "recent_signups": recent_signups,
        "active_today": active_today,
        "last_login_eastern": last_login_eastern,
        "pending_appeals": pending_appeals,
        "active_ip_bans": active_ip_bans
    })

@limiter.limit("4/hour")
# API endpoint to get users data
@app.get("/admin/api/users")
def get_users_admin_api(
    request: Request,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """API endpoint to get users with enhanced info including IPs"""
    try:
        # Get users with their data
        users = db.query(User).order_by(User.created_at.desc()).limit(limit).all()
        
        users_data = []
        for user in users:
            # Calculate statistics
            total_tweets = db.query(GeneratedTweet).filter(
                GeneratedTweet.user_id == user.id
            ).count()
            
            # Format dates in Eastern Time
            created_at_eastern = convert_to_eastern(user.created_at) if user.created_at else None
            last_login_eastern = convert_to_eastern(user.last_login) if user.last_login else None
            suspended_at_eastern = convert_to_eastern(user.suspended_at) if hasattr(user, 'suspended_at') and user.suspended_at else None
            
            user_data = {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "plan": user.plan or "free",
                "is_active": user.is_active,
                "is_suspended": user.is_suspended,
                "suspension_reason": user.suspension_reason,
                "suspended_at": user.suspended_at.isoformat() if user.suspended_at else None,
                "suspended_at_eastern": suspended_at_eastern,
                "suspended_by": user.suspended_by,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "created_at_eastern": created_at_eastern,
                "last_login": user.last_login.isoformat() if user.last_login else None,
                "last_login_eastern": last_login_eastern,
                "total_tweets": total_tweets,
                "last_known_ip": getattr(user, 'last_known_ip', None),
                "registration_ip": getattr(user, 'registration_ip', None),
                "is_ip_banned": getattr(user, 'is_ip_banned', False),
                "failed_login_attempts": getattr(user, 'failed_login_attempts', 0),
                "account_locked": bool(getattr(user, 'account_locked_until', None) and user.account_locked_until > datetime.utcnow())

            }
            users_data.append(user_data)
        
        return JSONResponse({
            "success": True,
            "users": users_data,
            "total": len(users_data)
        })
        
    except Exception as e:
        print(f"Error fetching users: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)
    
@app.get("/admin/api/ip-bans")
def get_ip_bans(admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    """Get all active IP bans (API endpoint)"""
    try:
        active_bans = db.query(IPban).filter(
            IPban.is_active == True,
            (IPban.expires_at.is_(None)) | (IPban.expires_at > datetime.utcnow())
        ).order_by(IPban.banned_at.desc()).all()
        
        bans_data = []
        for ban in active_bans:
            bans_data.append({
                "id": ban.id,
                "ip_address": ban.ip_address,
                "reason": ban.reason,
                "banned_by": ban.banned_by,
                "banned_at": ban.banned_at.isoformat(),
                "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
                "is_permanent": ban.expires_at is None
            })
        
        return {
            "success": True,
            "bans": bans_data,
            "total": len(bans_data)
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

def validate_ip_address(ip_str: str) -> bool:
    """Validate if string is a valid IP address (IPv4 or IPv6)"""
    try:
        ipaddress.ip_address(ip_str.strip())
        return True
    except ValueError:
        return False

def is_ip_banned(ip_address: str, db: Session) -> bool:
    """Check if an IP address is currently banned"""
    active_ban = db.query(IPban).filter(
        IPban.ip_address == ip_address,
        IPban.is_active == True,
        (IPban.expires_at.is_(None)) | (IPban.expires_at > datetime.utcnow())
    ).first()
    
    return active_ban is not None

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def check_recent_errors():
    """Stub for recent error checking - returns empty list or dummy data"""
    # You can implement actual error log reading here if needed
    return []

@app.get("/admin/health-check")
def admin_health_check(admin: User = Depends(get_admin_user)):
    db = SessionLocal()
    try:
        # Check database
        user_count = db.query(User).count()
        
        # Check recent errors
        recent_errors = check_recent_errors()
        
        return {
            "status": "healthy",
            "user_count": user_count,
            "recent_errors": recent_errors,
            "timestamp": datetime.utcnow()
        }
    finally:
        db.close()

@app.get("/admin/ban-ip", response_class=HTMLResponse)
def ban_ip_page(request: Request, admin: User = Depends(get_admin_user)):
    """Display IP ban management page"""
    db = SessionLocal()
    try:
        # Get all active bans
        active_bans = db.query(IPban).filter(
            IPban.is_active == True,
            (IPban.expires_at.is_(None)) | (IPban.expires_at > datetime.utcnow())
        ).order_by(IPban.banned_at.desc()).all()
        
        return templates.TemplateResponse("admin/ban-ip.html", {
            "request": request,
            "active_bans": active_bans,
            "admin": admin
        })
    finally:
        db.close()

@limiter.limit("1/hour")
@app.post("/admin/ban-ip")
async def ban_ip_address(
    request: Request,
    ip_address: str = Form(...),
    reason: str = Form(...),
    duration_hours: int = Form(None),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Ban an IP address"""
    try:
        # Validate IP address
        if not validate_ip_address(ip_address):
            return templates.TemplateResponse("admin/ban-ip.html", {
                "request": request,
                "error": "Invalid IP address format",
                "active_bans": db.query(IPban).filter(IPban.is_active == True).all(),
                "admin": admin
            })
        
        # Clean IP address
        clean_ip = ip_address.strip()
        
        # Check if IP is already banned
        existing_ban = db.query(IPban).filter(
            IPban.ip_address == clean_ip,
            IPban.is_active == True,
            (IPban.expires_at.is_(None)) | (IPban.expires_at > datetime.utcnow())
        ).first()
        
        if existing_ban:
            return templates.TemplateResponse("admin/ban-ip.html", {
                "request": request,
                "error": f"IP {clean_ip} is already banned",
                "active_bans": db.query(IPban).filter(IPban.is_active == True).all(),
                "admin": admin
            })
        
        # Calculate expiry time
        expires_at = None
        if duration_hours and duration_hours > 0:
            expires_at = datetime.utcnow() + timedelta(hours=duration_hours)
        
        # Create ban record
        ip_ban = IPban(
            ip_address=clean_ip,
            reason=reason.strip(),
            banned_by=admin.email,
            expires_at=expires_at
        )
        
        db.add(ip_ban)
        db.commit()
        
        # Log the ban
        ban_type = f"for {duration_hours} hours" if duration_hours else "permanently"
        print(f"🚫 IP {clean_ip} banned {ban_type} by {admin.email}: {reason}")
        
        return RedirectResponse("/admin/ban-ip?success=banned", status_code=302)
        
    except Exception as e:
        print(f"Error banning IP: {str(e)}")
        db.rollback()
        return templates.TemplateResponse("admin/ban-ip.html", {
            "request": request,
            "error": f"Failed to ban IP: {str(e)}",
            "active_bans": db.query(IPban).filter(IPban.is_active == True).all(),
            "admin": admin
        })

@limiter.limit("1/hour")
@app.post("/admin/unban-ip")
async def unban_ip_address(
    request: Request,
    ban_id: int = Form(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Unban an IP address"""
    try:
        # Find the ban record
        ip_ban = db.query(IPban).filter(IPban.id == ban_id).first()
        
        if not ip_ban:
            return templates.TemplateResponse("admin/ban-ip.html", {
                "request": request,
                "error": "Ban record not found",
                "active_bans": db.query(IPban).filter(IPban.is_active == True).all(),
                "admin": admin
            })
        
        # Mark as inactive
        ip_ban.is_active = False
        ip_ban.unbanned_at = datetime.utcnow()
        ip_ban.unbanned_by = admin.email
        
        db.commit()
        
        # Log the unban
        print(f"✅ IP {ip_ban.ip_address} unbanned by {admin.email}")
        
        return RedirectResponse("/admin/ban-ip?success=unbanned", status_code=302)
        
    except Exception as e:
        print(f"Error unbanning IP: {str(e)}")
        db.rollback()
        return templates.TemplateResponse("admin/ban-ip.html", {
            "request": request,
            "error": f"Failed to unban IP: {str(e)}",
            "active_bans": db.query(IPban).filter(IPban.is_active == True).all(),
            "admin": admin
        })
    
def cleanup_expired_bans():
    """Mark expired bans as inactive"""
    db = SessionLocal()
    try:
        expired_bans = db.query(IPban).filter(
            IPban.is_active == True,
            IPban.expires_at.is_not(None),
            IPban.expires_at <= datetime.utcnow()
        ).all()
        
        for ban in expired_bans:
            ban.is_active = False
            ban.unbanned_at = datetime.utcnow()
            ban.unbanned_by = "system_auto_expire"
        
        if expired_bans:
            db.commit()
            print(f"🧹 Cleaned up {len(expired_bans)} expired IP bans")
            
    except Exception as e:
        print(f"Error cleaning up expired bans: {str(e)}")
        db.rollback()
    finally:
        db.close()

def get_real_client_ip(request: Request) -> str:
    """
    Get the real client IP address, handling various proxy scenarios
    """
    # List of headers to check in order of preference
    ip_headers = [
        "CF-Connecting-IP",      # Cloudflare
        "X-Forwarded-For",       # Standard proxy header
        "X-Real-IP",             # Nginx proxy
        "X-Client-IP",           # Apache proxy  
        "X-Forwarded",           # Less common
        "Forwarded-For",         # Less common
        "Forwarded",             # RFC 7239
    ]
    
    # Check each header for a valid IP
    for header in ip_headers:
        ip_value = request.headers.get(header)
        if ip_value:
            # X-Forwarded-For can contain multiple IPs: "client, proxy1, proxy2"
            # We want the first (leftmost) IP which is the original client
            if header == "X-Forwarded-For":
                ip_list = [ip.strip() for ip in ip_value.split(',')]
                for ip in ip_list:
                    if is_valid_public_ip(ip):
                        print(f"✅ Found real client IP from {header}: {ip}")
                        return ip
            else:
                if is_valid_public_ip(ip_value.strip()):
                    print(f"✅ Found real client IP from {header}: {ip_value}")
                    return ip_value.strip()
    
    # Fallback to request.client.host
    fallback_ip = request.client.host if request.client else "Unknown"
    print(f"⚠️  Using fallback IP: {fallback_ip}")
    return fallback_ip

def is_valid_public_ip(ip_str: str) -> bool:
    """
    Check if an IP address is valid and public (not private/local)
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        
        # Check if it's a valid IP and not private
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            print(f"❌ IP {ip_str} is private/local, skipping")
            return False
            
        return True
        
    except ValueError:
        print(f"❌ Invalid IP format: {ip_str}")
        return False
    
@limiter.limit("1/hour")
@app.post("/admin/suspend-user")
async def suspend_user_enhanced(
    request: Request,
    user_id: int = Form(...),
    reason: str = Form(...),
    ban_ip: bool = Form(default=False),
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """Suspend user with optional IP ban"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return JSONResponse({
                "success": False,
                "message": "User not found"
            }, status_code=404)
        
        if user.email in ADMIN_USERS:
            return JSONResponse({
                "success": False,
                "message": "Cannot suspend admin accounts."
            }, status_code=403)
        
        # Suspend the user
        user.is_suspended = True
        user.suspension_reason = reason
        user.suspended_at = datetime.utcnow()
        user.suspended_by = admin_user.email
        
        # Ban IP if requested
        if ban_ip and hasattr(user, 'last_known_ip') and user.last_known_ip:
            ban_ip_address(
                user.last_known_ip,
                f"User suspension: {reason}",
                admin_user.email,
                db
            )
        
        db.commit()
        
        # Send suspension email
        try:
            await email_service.send_suspension_email(user.email, reason)
        except Exception as e:
            print(f"Failed to send suspension email: {e}")
        
        message = f"User {user.username} has been suspended"
        if ban_ip:
            message += f" and IP {user.last_known_ip} has been banned"
        
        return JSONResponse({
            "success": True,
            "message": message
        })
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "message": f"Error: {str(e)}"
        }, status_code=500)

@limiter.limit("1/hour")
@app.post("/admin/unsuspend-user")
async def unsuspend_user_enhanced(
    request: Request,
    user_id: int = Form(...),
    unban_ip: bool = Form(default=False),
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """Unsuspend user with optional IP unban"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return JSONResponse({
                "success": False,
                "message": "User not found"
            }, status_code=404)
        
        # Unsuspend the user
        user.is_suspended = False
        user.suspension_reason = None
        user.suspended_at = None
        user.suspended_by = None
        
        # Unban IP if requested
        if unban_ip and hasattr(user, 'last_known_ip') and user.last_known_ip:
            unban_ip_address(user.last_known_ip, db)
            user.is_ip_banned = False
        
        db.commit()
        
        # Send restoration email
        try:
            await email_service.send_account_restored_email(user.email)
        except Exception as e:
            print(f"Failed to send restoration email: {e}")
        
        message = f"User {user.username} has been unsuspended"
        if unban_ip:
            message += f" and IP {user.last_known_ip} has been unbanned"
        
        return JSONResponse({
            "success": True,
            "message": message
        })
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "message": f"Error: {str(e)}"
        }, status_code=500)
    
 # View suspension appeals
@app.get("/admin/appeals")
def view_suspension_appeals(
    request: Request,
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """View all suspension appeals"""
    appeals = db.query(SuspensionAppeal).order_by(
        SuspensionAppeal.created_at.desc()
    ).all()
    
    return templates.TemplateResponse("admin/appeals.html", {
        "request": request,
        "user": admin_user,
        "appeals": appeals
    })

@limiter.limit("1/hour")
# Process suspension appeal
@app.post("/admin/process-appeal")
async def process_suspension_appeal(
    request: Request,
    appeal_id: int = Form(...),
    action: str = Form(...),  # "approve" or "deny"
    admin_notes: str = Form(default=""),
    db: Session = Depends(get_db),
    admin_user = Depends(get_admin_user)
):
    """Process (approve/deny) a suspension appeal"""
    try:
        appeal = db.query(SuspensionAppeal).filter(
            SuspensionAppeal.id == appeal_id
        ).first()
        
        if not appeal:
            return JSONResponse({
                "success": False,
                "message": "Appeal not found"
            }, status_code=404)
        
        appeal.status = "approved" if action == "approve" else "denied"
        appeal.reviewed_at = datetime.utcnow()
        appeal.reviewed_by = admin_user.email
        appeal.admin_notes = admin_notes.strip()
        
        if action == "approve":
            # Restore user account
            user = appeal.user
            user.is_suspended = False
            user.suspension_reason = None
            user.suspended_at = None
            user.suspended_by = None
            
            # Send approval email
            try:
                await email_service.send_appeal_approved_email(
                    user.email, user.username, admin_notes
                )
            except Exception as e:
                print(f"Failed to send appeal approval email: {e}")
        else:
            # Send denial email
            try:
                await email_service.send_appeal_denied_email(
                    appeal.email, appeal.name, admin_notes
                )
            except Exception as e:
                print(f"Failed to send appeal denial email: {e}")
        
        db.commit()
        
        return JSONResponse({
            "success": True,
            "message": f"Appeal has been {appeal.status}"
        })
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "message": f"Error: {str(e)}"
        }, status_code=500)

@app.post("/admin/appeals/{appeal_id}/approve")
async def approve_appeal(
    appeal_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Approve a suspension appeal and unsuspend the user"""
    appeal = db.query(SuspensionAppeal).filter(SuspensionAppeal.id == appeal_id).first()
    if not appeal:
        raise HTTPException(status_code=404, detail="Appeal not found")
    
    # Unsuspend the user
    await unsuspend_user_account(appeal.user_id, db)
    
    # Update appeal status
    appeal.status = "approved"
    appeal.reviewed_by = admin.username
    appeal.reviewed_at = datetime.utcnow()
    db.commit()
    
    return RedirectResponse("/admin/appeals", status_code=302)

@app.post("/admin/appeals/{appeal_id}/deny")
async def deny_appeal(
    appeal_id: int,
    denial_reason: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Deny a suspension appeal"""
    appeal = db.query(SuspensionAppeal).filter(SuspensionAppeal.id == appeal_id).first()
    if not appeal:
        raise HTTPException(status_code=404, detail="Appeal not found")
    
    # Update appeal status
    appeal.status = "denied"
    appeal.reviewed_by = admin.username
    appeal.reviewed_at = datetime.utcnow()
    db.commit()
    
    # Send denial email to user
    try:
        await email_service.send_appeal_denial_email(appeal.user, denial_reason)
    except Exception as e:
        print(f"Failed to send appeal denial email: {str(e)}")
    
    return RedirectResponse("/admin/appeals", status_code=302)

# Update the current user check to redirect suspended users
def get_current_user_with_suspension_check(request: Request):
    """Modified get_current_user that redirects suspended users"""
    try:
        user = get_current_user(request)
        
        # If user is suspended, redirect to suspended page
        if user.is_suspended:
            # Instead of raising exception, we'll handle this in routes
            return user
        
        return user
        
    except HTTPException:
        raise

# Middleware to handle suspended users globally
@app.middleware("http") 
async def suspension_redirect_middleware(request: Request, call_next):
    """Redirect suspended users to suspension page"""
    
    # Skip for certain routes
    skip_routes = [
        "/static", "/suspended", "/logout", "/favicon.ico", 
        "/_health", "/admin", "/contact"
    ]
    
    if any(request.url.path.startswith(route) for route in skip_routes):
        return await call_next(request)
    
    # Check if user is logged in and suspended
    try:
        token = request.cookies.get("access_token")
        if token:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
            
            if username:
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.username == username).first()
                    if user and user.is_suspended:
                        # Redirect to suspended page
                        return RedirectResponse("/suspended", status_code=302)
                finally:
                    db.close()
    except:
        pass
    
    return await call_next(request)

@limiter.limit("1/hour")
# Updated force password reset endpoint
@app.post("/admin/force-password-reset")
async def force_password_reset_endpoint(
    request: Request,
    user_id: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Force a user to reset their password"""
    # Check if user is admin
    if not current_user or current_user.email not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Create password reset record
    reset_record = create_password_reset_record(user.id, db)
    
    # Send password reset email
    try:
        await email_service.send_password_reset_email(
            user, 
            reset_record.token, 
            request.client.host if request.client else "Admin Panel"
        )
        
        # Log the activity
        log_user_activity(
            user_id=user.id,
            activity_type="password_reset_forced",
            description="Password reset forced by admin",
            ip_address=request.client.host if request.client else None,
            user_metadata={"admin": current_user.email},
            db=db
        )
        
        return {"message": "Password reset email sent"}
    except Exception as e:
        print(f"Failed to send password reset email: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to send password reset email")
    
@app.get("/admin/user/{user_id}")
async def get_user_details_api(
    user_id: int,
    request: Request
):
    """Get detailed user information for admin modal with Eastern Time"""
    db = None
    try:
        db = SessionLocal()
        
        # Check admin authorization
        user = get_optional_user(request)
        if not is_admin_user(user):
            return JSONResponse({
                "success": False,
                "error": "Admin access required"
            }, status_code=403)
        
        # Get user
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            return JSONResponse({
                "success": False,
                "error": "User not found"
            }, status_code=404)
        
        # Get user statistics
        total_tweets = 0
        monthly_tweets = 0
        weekly_tweets = 0
        
        # Check if GeneratedTweet table exists and get stats
        try:
            total_tweets = db.query(GeneratedTweet).filter(GeneratedTweet.user_id == user_id).count()
            
            # Monthly tweets (last 30 days)
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            monthly_tweets = db.query(GeneratedTweet).filter(
                GeneratedTweet.user_id == user_id,
                GeneratedTweet.generated_at >= thirty_days_ago
            ).count()
            
            # Weekly tweets (last 7 days)
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            weekly_tweets = db.query(GeneratedTweet).filter(
                GeneratedTweet.user_id == user_id,
                GeneratedTweet.generated_at >= seven_days_ago
            ).count()
        except Exception as tweet_error:
            print(f"Error getting tweet stats: {tweet_error}")
            # Keep defaults at 0
        
        # Build user data with Eastern Time conversion
        user_data = {
            "id": target_user.id,
            "username": target_user.username,
            "email": target_user.email,
            "plan": target_user.plan,
            "is_suspended": getattr(target_user, 'is_suspended', False),
            "suspension_reason": getattr(target_user, 'suspension_reason', None),
            "suspended_by": getattr(target_user, 'suspended_by', None),
            "is_active": target_user.is_active,
            "stripe_customer_id": getattr(target_user, 'stripe_customer_id', None),
            "original_plan": getattr(target_user, 'original_plan', None),
            "api_key": bool(getattr(target_user, 'api_key', None)),
            "total_tweets": total_tweets,
            "monthly_tweets": monthly_tweets,
            "weekly_tweets": weekly_tweets,
            "role": getattr(target_user, 'role', None),
            "industry": getattr(target_user, 'industry', None),
            "goals": getattr(target_user, 'goals', None),
            "posting_frequency": getattr(target_user, 'posting_frequency', None)
        }
        
        # Handle datetime fields with Eastern Time conversion
        if target_user.created_at:
            user_data["created_at"] = target_user.created_at.isoformat()
            user_data["created_at_eastern"] = convert_to_eastern(target_user.created_at)
        else:
            user_data["created_at"] = None
            user_data["created_at_eastern"] = "Unknown"
        
        if hasattr(target_user, 'last_login') and target_user.last_login:
            user_data["last_login"] = target_user.last_login.isoformat()
            user_data["last_login_eastern"] = convert_to_eastern(target_user.last_login)
        else:
            user_data["last_login"] = None
            user_data["last_login_eastern"] = "Never"
        
        if hasattr(target_user, 'suspended_at') and target_user.suspended_at:
            user_data["suspended_at"] = target_user.suspended_at.isoformat()
            user_data["suspended_at_eastern"] = convert_to_eastern(target_user.suspended_at)
        else:
            user_data["suspended_at"] = None
            user_data["suspended_at_eastern"] = None
        
        # Add last password change if available
        if hasattr(target_user, 'last_password_change') and target_user.last_password_change:
            user_data["last_password_change"] = target_user.last_password_change.isoformat()
            user_data["last_password_change_eastern"] = convert_to_eastern(target_user.last_password_change)
        else:
            user_data["last_password_change"] = None
            user_data["last_password_change_eastern"] = "Never"
        
        # Add security info
        user_data["failed_login_attempts"] = getattr(target_user, 'failed_login_attempts', 0)
        
        if hasattr(target_user, 'account_locked_until') and target_user.account_locked_until:
            user_data["account_locked"] = target_user.account_locked_until > datetime.utcnow()
            user_data["account_locked_until_eastern"] = convert_to_eastern(target_user.account_locked_until)
        else:
            user_data["account_locked"] = False
            user_data["account_locked_until_eastern"] = None
        
        db.close()
        
        return JSONResponse({
            "success": True,
            "user": user_data
        })
        
    except Exception as e:
        if db:
            try:
                db.close()
            except:
                pass
        
        print(f"Error getting user details: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)
    

async def unsuspend_user_account(user_id: int, db: Session):
    """Unsuspend a user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if not user.is_suspended:
        return {"message": "User is not suspended", "user": user}
    
    # Clear suspension fields
    old_reason = user.suspension_reason
    user.is_suspended = False
    user.suspension_reason = None
    user.suspended_at = None
    user.suspended_by = None
    
    try:
        db.commit()
        
        # Log the action
        print(f"✅ User {user.username} (ID: {user.id}) unsuspended")
        print(f"   Previous reason: {old_reason}")
        
        # Send restoration email
        try:
            await email_service.send_account_restored_email(user.email)
            print(f"✅ Account restored notification sent to {user.email}")
        except Exception as e:
            print(f"❌ Failed to send restoration email: {str(e)}")
        
        return {"message": "User unsuspended successfully", "user": user}
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error unsuspending user: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to unsuspend user")

# Add these helper functions after your existing helper functions
def validate_ip_address(ip_str: str) -> bool:
    """Validate if string is a valid IP address (IPv4 or IPv6)"""
    try:
        ipaddress.ip_address(ip_str.strip())
        return True
    except ValueError:
        return False

def is_ip_banned(ip_address: str, db) -> bool:
    """Check if an IP address is currently banned"""
    try:
        active_ban = db.query(IPban).filter(
            IPban.ip_address == ip_address,
            IPban.is_active == True,
            (IPban.expires_at.is_(None)) | (IPban.expires_at > datetime.utcnow())
        ).first()
        
        return active_ban is not None
    except Exception as e:
        print(f"Error checking IP ban: {str(e)}")
        return False

# Add dependency to get database session (if you don't have it already)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def log_login_attempt(user_id: int, ip_address: str, success: bool, db: Session):
    """Log login attempts with IP addresses"""
    try:
        # You could create a LoginAttempt model for this
        print(f"Login attempt: User {user_id} from {ip_address} - {'Success' if success else 'Failed'}")
    except Exception as e:
        print(f"Error logging login attempt: {str(e)}")
        
@app.get("/admin/user/{user_id}/activity")
async def admin_user_activity(
    user_id: int,
    request: Request,
    page: int = Query(default=1, ge=1),
    current_user: User = Depends(get_current_user)
):
    """Get user activity log with Eastern Time conversion"""
    
    if current_user.email not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    db = SessionLocal()
    try:
        # Get the user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Try to get real activity data first
        real_activities = []
        try:
            activities = db.query(UserActivity).filter(
                UserActivity.user_id == user_id
            ).order_by(UserActivity.timestamp.desc()).limit(50).all()
            
            for activity in activities:
                real_activities.append({
                    "id": activity.id,
                    "type": activity.activity_type,
                    "description": activity.description,
                    "timestamp": activity.timestamp,
                    "ip_address": activity.ip_address,  # Real IP from database
                    "metadata": json.loads(activity.user_metadata) if activity.user_metadata else {}
                })
        except Exception as e:
            print(f"Could not get real activities: {e}")
        
        # If no real activities, create sample data with REAL IPs from user record
        if not real_activities:
            sample_activities = [
                {
                    "id": 1,
                    "type": "login",
                    "description": "User logged in",
                    "timestamp": user.last_login or datetime.utcnow(),
                    "ip_address": getattr(user, 'last_known_ip', None) or "Unknown",  # REAL IP
                    "metadata": {"user_agent": "Unknown"}
                },
                {
                    "id": 2,
                    "type": "account_created",
                    "description": "Account created",
                    "timestamp": user.created_at,
                    "ip_address": getattr(user, 'registration_ip', None) or "Unknown",  # REAL IP
                    "metadata": {"signup_method": "email"}
                }
            ]
            activities_to_use = sample_activities
        else:
            activities_to_use = real_activities
        
        # Convert timestamps to Eastern Time
        for activity in activities_to_use:
            activity["timestamp_eastern"] = convert_to_eastern(activity["timestamp"])
            activity["timestamp"] = activity["timestamp"].isoformat()
        
        return {
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "created_at_eastern": convert_to_eastern(user.created_at),
                "last_login": convert_to_eastern(user.last_login)
            },
            "activities": activities_to_use,
            "pagination": {
                "page": page,
                "pages": 1,
                "total": len(activities_to_use)
            },
            "timezone": "America/New_York" if TIMEZONE_AVAILABLE else "UTC"
        }
        
    finally:
        db.close()

@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    user = get_optional_user(request)
    response = templates.TemplateResponse("login.html", {
        "request": request, 
        "user": user
    })
    return response
 
@limiter.limit("5/minute")
@app.post("/login")
async def login_post(
    request: Request, 
    username: str = Form(...), 
    password: str = Form(...),
):
    db = SessionLocal()
    try:
        # Get real client IP
        client_ip = get_real_client_ip(request)
        print(f"🔐 Login attempt from IP: {client_ip}")
        
        # Get user record first (for failed attempt tracking)
        user_record = db.query(User).filter(
            (User.username == username) | (User.email == username)
        ).first()
        
        # Check if account is temporarily locked
        if user_record and user_record.account_locked_until:
            if user_record.account_locked_until > datetime.utcnow():
                time_remaining = user_record.account_locked_until - datetime.utcnow()
                hours_remaining = int(time_remaining.total_seconds() / 3600) + 1
                response = templates.TemplateResponse("login.html", {
                    "request": request,
                    "user": None,
                    "error": f"Account temporarily locked. Try again in {hours_remaining} hour(s).",
                })
                return response
            else:
                # Lock period has expired, clear it
                user_record.account_locked_until = None
                user_record.failed_login_attempts = 0
                db.commit()
        
        # Authenticate user
        user = authenticate_user(db, username, password)
        
        if user:  # successful login
            user.last_known_ip = client_ip
            user.last_login = datetime.utcnow()
            db.commit()
            
        if not user:
            # Track failed login attempts
            if user_record:
                user_record.failed_login_attempts = (user_record.failed_login_attempts or 0) + 1
                user_record.last_failed_login = datetime.utcnow()
                
                # Lock account after 4 failed attempts
                if user_record.failed_login_attempts >= 4:
                    user_record.account_locked_until = datetime.utcnow() + timedelta(hours=24)
                    db.commit()
                    
                    try:
                        await email_service.send_account_locked_email(user_record.email, 24)
                        print(f"✅ Account locked email sent to {user_record.email}")
                    except Exception as e:
                        print(f"❌ Failed to send account locked email: {e}")
                    
                    response = templates.TemplateResponse("login.html", {
                        "request": request,
                        "user": None,
                        "error": "Account locked due to multiple failed login attempts. Check your email for recovery instructions."
                    })
                    return response
                else:
                    attempts_left = 4 - user_record.failed_login_attempts
                    db.commit()
                    
                    response = templates.TemplateResponse("login.html", {
                        "request": request,
                        "user": None,
                        "error": f"Invalid credentials. {attempts_left} attempt(s) remaining before account lock."
                    })
                    return response
            else:
                # Username/email doesn't exist
                response = templates.TemplateResponse("login.html", {
                    "request": request,
                    "user": None,
                    "error": "Invalid credentials",
                })
                return response
        
        # Check if account is suspended
        if user.is_suspended:
            response = templates.TemplateResponse("login.html", {
                "request": request,
                "user": None,
                "error": f"Account suspended: {user.suspension_reason or 'Please contact support'}"
            })
            return response
        
        # Check if email is verified
        if not user.is_active:
            response = templates.TemplateResponse("login.html", {
                "request": request, 
                "user": None,
                "error": "Please verify your email before logging in"
            })
            return response
        
        # Successful login - reset failed attempts
        user.failed_login_attempts = 0
        user.last_failed_login = None
        user.account_locked_until = None
        db.commit()
        
        # Create access token
        access_token = create_access_token(
            data={"sub": user.username},
            expires_delta=timedelta(days=2)
        )
        
        print(f"✅ Successful login for user: {user.username} at {datetime.utcnow()}")
        
        response = RedirectResponse("/dashboard", status_code=302)
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
             secure=IS_PRODUCTION,  
            samesite='lax',
            max_age=2*24*3600,
            domain=".giverai.me"
        )
        return response
        
    except Exception as e:
        print(f"Login error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        response = templates.TemplateResponse("login.html", {
            "request": request,
            "user": None, 
            "error": "Invalid credentials"
        })
        return response
    finally:
        db.close()

# Optional: Add a route to unlock accounts manually
@app.get("/unlock-account")
async def unlock_account_page(request: Request):
    """Page for users to request account unlock"""
    return templates.TemplateResponse("unlock_account.html", {
        "request": request,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })

@app.post("/unlock-account")
async def unlock_account_request(
    request: Request,
    email_or_username: str = Form(...),
    g_recaptcha_response: str = Form(alias="g-recaptcha-response", default="")
):
    """Allow users to request account unlock"""
    if not verify_recaptcha(g_recaptcha_response):
        return templates.TemplateResponse("unlock_account.html", {
            "request": request,
            "error": "Invalid credentials",
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
    
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            (User.username == email_or_username) | (User.email == email_or_username)
        ).first()
        
        if user and user.account_locked_until and user.account_locked_until > datetime.utcnow():
            # Clear the lock
            user.account_locked_until = None
            user.failed_login_attempts = 0
            db.commit()
            
            return templates.TemplateResponse("unlock_account.html", {
                "request": request,
                "success": "Account unlocked successfully. You can now log in.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })
        else:
            # Don't reveal whether account exists or is locked
            return templates.TemplateResponse("unlock_account.html", {
                "request": request,
                "success": "If your account was locked, it has been unlocked.",
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })
    finally:
        db.close

@app.get("/logout")
def logout(request: Request):
    # Render the logout page with deleted cookies
    response = templates.TemplateResponse("logout.html", {"request": request})
    
    # Server-side cookie deletion
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("playground_count", path="/")
    
    # Prevent any caching
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    return response

@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):  # ← Removed csrf_protect parameter
    user = get_optional_user(request)
    # csrf_response = csrf_protect.generate_csrf()  ← COMMENT OUT
    # csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response  ← COMMENT OUT
    
    return templates.TemplateResponse("contact.html", {  # ← Changed to simple return
        "request": request,
        "user": user,
        "form_data": {}
        # "csrf_token": csrf_token  ← COMMENT OUT (remove from dict)
    })
    # response.set_cookie(...)  ← COMMENT OUT entire cookie section

@limiter.limit("10/minute")
@app.post("/contact", response_class=HTMLResponse)
async def handle_contact_form(request: Request):  # ← Removed csrf_protect parameter
    # await csrf_protect.validate_csrf(request)  ← COMMENT OUT
    user = get_optional_user(request)
    form_data = {}
    
    try:
        form = await request.form()
        form_data = {
            "name": sanitize_input(form["name"]),
            "email": sanitize_input(form["email"]),
            "subject": form["subject"],
            "message": sanitize_input(form["message"])
        }
        
        # Validation
        if not all([form_data["name"], form_data["email"], form_data["subject"], form_data["message"]]):
            raise ValueError("All required fields must be filled out")
        
        if len(form_data["message"]) < 10:
            raise ValueError("Please provide a more detailed message")
        
        user_info = None
        if user:
            user_info = f"Username: {user.username} | Plan: {user.plan.replace('_', ' ').title()} | Member since: {user.created_at.strftime('%Y-%m-%d')}"
        
        # Send emails
        support_sent = email_service.send_contact_form_notification(
            form_data["name"], form_data["email"], form_data["subject"], form_data["message"], user_info
        )
        
        confirmation_sent = email_service.send_contact_confirmation_email(
            form_data["name"], form_data["email"], form_data["subject"]
        )
        
        if support_sent and confirmation_sent:
            return templates.TemplateResponse("contact.html", {
                "request": request,
                "user": user,
                "form_data": {},
                "success": "Thank you! Your message has been sent successfully. We'll get back to you within 24 hours."
            })
        else:
            raise Exception("Failed to send email")
            
    except ValueError as e:
        return templates.TemplateResponse("contact.html", {
            "request": request,
            "user": user,
            "form_data": form_data,
            "error": str(e)
        })
    except Exception as e:
        print(f"Contact form error: {e}")
        return templates.TemplateResponse("contact.html", {
            "request": request,
            "user": user,
            "form_data": form_data,
            "error": "Sorry, there was an issue sending your message. Please try again or email us directly at support@giverai.me"
        })
        
@app.get("/faq", response_class=HTMLResponse)
def faq_page(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("faq.html", {
        "request": request, 
        "user": user
    })

@app.get("/suspended", response_class=HTMLResponse)
def suspended_page_get(request: Request):
    """Display suspended account page"""
    # Use the special function that allows suspended users
    user = get_optional_suspended_user(request)
    
    # If no user or user is not suspended, redirect to dashboard/login
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    if not user.is_suspended:
        return RedirectResponse("/dashboard", status_code=302)
    
    return templates.TemplateResponse("suspended.html", {
        "request": request,
        "user": user,
        "success": None,
        "error": None,
        "form_data": None,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })
def safe_user_data(user):
    """Safely return user data for templates, handling None values"""
    if not user:
        return {
            'username': 'Unknown',
            'email': 'Unknown',
            'plan': 'free',
            'suspended_at': None,
            'suspension_reason': None,
            'is_suspended': False
        }
    
    return {
        'username': getattr(user, 'username', 'Unknown'),
        'email': getattr(user, 'email', 'Unknown'),
        'plan': getattr(user, 'plan', 'free'),
        'suspended_at': getattr(user, 'suspended_at', None),
        'suspension_reason': getattr(user, 'suspension_reason', None),
        'is_suspended': getattr(user, 'is_suspended', False)
    }

def safe_user_data(user):
    """Safely return user data for templates, handling None values"""
    if not user:
        return {
            'username': 'Unknown',
            'email': 'Unknown',
            'plan': 'free',
            'suspended_at': None,
            'suspension_reason': None,
            'is_suspended': False
        }
    
    return {
        'username': getattr(user, 'username', 'Unknown'),
        'email': getattr(user, 'email', 'Unknown'),
        'plan': getattr(user, 'plan', 'free'),
        'suspended_at': getattr(user, 'suspended_at', None),
        'suspension_reason': getattr(user, 'suspension_reason', None),
        'is_suspended': getattr(user, 'is_suspended', False)
    }

# Also, update your suspended route to use this helper:
@app.get("/suspended", response_class=HTMLResponse)
def suspended_page_get(request: Request):
    """Display suspended account page"""
    # Use the special function that allows suspended users
    user = get_optional_suspended_user(request)
    
    # If no user or user is not suspended, redirect to dashboard/login
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    if not user.is_suspended:
        return RedirectResponse("/dashboard", status_code=302)
    
    # Use safe user data to prevent None attribute errors
    user_data = safe_user_data(user)
    
    return templates.TemplateResponse("suspended.html", {
        "request": request,
        "user": user,  # Keep the original user object
        "user_data": user_data,  # Add safe user data
        "success": None,
        "error": None,
        "form_data": None,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })
    
# Handle suspension appeal form submission
@app.post("/suspended", response_class=HTMLResponse)
async def suspended_page_post(request: Request):
    """Handle suspension appeal form submission"""
    # Use the special function that allows suspended users
    user = get_optional_suspended_user(request)
    form_data = {}
    
    # Check if user exists and is actually suspended
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    if not user.is_suspended:
        return RedirectResponse("/dashboard", status_code=302)
    
    db = SessionLocal()
    try:
        form = await request.form()
        form_data = {
            "name": form.get("name", "").strip(),
            "email": form.get("email", "").strip(),
            "appeal_type": form.get("appeal_type", "").strip(),
            "appeal_message": form.get("appeal_message", "").strip(),
            "account_info": form.get("account_info", "").strip()
        }
        
        # Get reCAPTCHA response
        g_recaptcha_response = form.get("g-recaptcha-response", "")
        
        # Verify reCAPTCHA
        if not verify_recaptcha(g_recaptcha_response):
            return templates.TemplateResponse("suspended.html", {
                "request": request,
                "user": user,
                "error": "Please complete the reCAPTCHA verification",
                "success": None,
                "form_data": form_data,
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })
        
        # Validate form data
        if not form_data["name"]:
            raise ValueError("Name is required")
        if not form_data["email"]:
            raise ValueError("Email is required")
        if not form_data["appeal_type"]:
            raise ValueError("Appeal type is required")
        if not form_data["appeal_message"]:
            raise ValueError("Detailed explanation is required")
        if len(form_data["appeal_message"]) < 50:
            raise ValueError("Please provide a more detailed explanation (minimum 50 characters)")
        
        # Check if user has already submitted an appeal in the last 24 hours
        existing_appeal = db.query(SuspensionAppeal).filter(
            SuspensionAppeal.user_id == user.id,
            SuspensionAppeal.created_at > datetime.utcnow() - timedelta(hours=24)
        ).first()
        
        if existing_appeal:
            return templates.TemplateResponse("suspended.html", {
                "request": request,
                "user": user,
                "error": "You can only submit one appeal per 24 hours. Your previous appeal is still being reviewed.",
                "success": None,
                "form_data": form_data,
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })
        
        # Create new suspension appeal
        appeal = SuspensionAppeal(
            user_id=user.id,
            name=form_data["name"],
            email=form_data["email"],
            appeal_type=form_data["appeal_type"],
            appeal_message=form_data["appeal_message"]
        )
        
        db.add(appeal)
        db.commit()
        
        # Send appeal notification to admin
        try:
            await (user, appeal)
        except Exception as e:
            print(f"Failed to send suspension appeal notification: {str(e)}")
        
        # Send confirmation to user
        try:
            await send_appeal_confirmation_email(user, appeal)
        except Exception as e:
            print(f"Failed to send appeal confirmation: {str(e)}")
        
        # Clear form data on successful submission
        return templates.TemplateResponse("suspended.html", {
            "request": request,
            "user": user,
            "success": "Your appeal has been submitted successfully! Our team will review it within 24-48 hours and respond via email.",
            "error": None,
            "form_data": {},  # Clear form
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
        
    except ValueError as e:
        return templates.TemplateResponse("suspended.html", {
            "request": request,
            "user": user,
            "error": str(e),
            "success": None,
            "form_data": form_data,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
    except Exception as e:
        print(f"Error processing suspension appeal: {str(e)}")
        return templates.TemplateResponse("suspended.html", {
            "request": request,
            "user": user,
            "error": "An error occurred while submitting your appeal. Please try again.",
            "success": None,
            "form_data": form_data,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
    finally:
        db.close()

@app.middleware("http")
async def check_suspension_middleware(request: Request, call_next):
    """Middleware to check for suspended users and redirect them"""
    
    # Routes that suspended users should be able to access
    allowed_paths = [
        "/suspended",
        "/logout", 
        "/static/",
        "/contact",
        "/",
        "/login",
        "/register",
        "/forgot-password",
        "/reset-password",
        "/verify-email"
    ]
    
    # Skip middleware for these paths
    path = request.url.path
    if any(path.startswith(allowed_path) for allowed_path in allowed_paths):
        response = await call_next(request)
        return response
    
    # Skip for non-authenticated requests (no token)
    token = request.cookies.get("access_token")
    if not token:
        response = await call_next(request)
        return response
    
    # Check if user is suspended - use the allow_suspended version
    try:
        user = get_optional_suspended_user(request)
        if user and user.is_suspended:
            # Redirect suspended users to suspension page
            return RedirectResponse("/suspended", status_code=302)
    except Exception:
        # If we can't get the user, let the request proceed normally
        # The original route will handle the authentication error
        pass
    
    response = await call_next(request)
    return response

def update_database_for_suspension_appeals():
    """Create suspension appeals table and update user suspension_reason to TEXT"""
    engine = create_engine(DATABASE_URL)
    
    try:
        # Create the suspension appeals table
        Base.metadata.create_all(bind=engine)
        print("✅ Suspension appeals table created/verified")
        
        # Update suspension_reason column to TEXT if needed
        with engine.begin() as conn:
            try:
                conn.execute(text("ALTER TABLE users ALTER COLUMN suspension_reason TYPE TEXT"))
                print("✅ Updated suspension_reason to TEXT type")
            except Exception as e:
                # This might fail if it's already TEXT, which is fine
                print(f"Note: Could not update suspension_reason type: {e}")
                
    except Exception as e:
        print(f"❌ Error updating database for suspension appeals: {e}")

# Account Management Routes
@app.get("/account", response_class=HTMLResponse)
async def account(request: Request, user: User = Depends(get_current_user)):
    """Account page - original version with just better error handling"""
    # Get Stripe billing portal URL for paid users
    billing_portal_url = None
    if user.stripe_customer_id and user.plan not in ["free", "canceling"]:
        try:
            session = stripe.billing_portal.Session.create(
                customer=user.stripe_customer_id,
                return_url=str(request.url_for('account'))
            )
            billing_portal_url = session.url
        except Exception as e:
            print(f"Failed to create billing portal session: {e}")
    
    return templates.TemplateResponse("account.html", {
        "request": request,
        "user": user,
        "billing_portal_url": billing_portal_url
    })

# Fix history route
@app.get("/history", response_class=HTMLResponse)
def tweet_history(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Use user.features instead of get_plan_features()
        days = user.features["history_days"]
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
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

@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_get(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("onboarding.html", {"request": request, "user": user})

@app.post("/onboarding", response_class=HTMLResponse)
def onboarding_post(request: Request,
                   role: str = Form(...),
                   industry: str = Form(...),
                   goals: str = Form(...),
                   posting_frequency: str = Form(...)):
    db = SessionLocal()
    try:
        user = get_optional_user(request)  
        if user:
            # Update existing user
            db_user = db.query(User).filter(User.id == user.id).first()
            if db_user:
                db_user.role = role
                db_user.industry = industry
                db_user.goals = goals
                db_user.posting_frequency = posting_frequency
                db.commit()
                print(f"Updated onboarding for user: {db_user.username}")
                return RedirectResponse("/dashboard", status_code=302)
        
        # If no user (new registration flow), redirect to login
        return RedirectResponse("/login", status_code=302)
    finally:
        db.close()

@app.post("/account/change_password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    csrf_protect: CsrfProtect = Depends(),
    user: User = Depends(get_current_user)
):
    await csrf_protect.validate_csrf(request)
    db = SessionLocal()
    try:
        # Get IP address from request
        ip_address = get_real_client_ip(request) if request.client else "Unknown"

        # Load user from DB for verification
        db_user = db.query(User).filter(User.id == user.id).first()

        # Verify current password
        if not verify_password(current_password, db_user.hashed_password):
            db_user = apply_plan_features(db_user)
            return templates.TemplateResponse("account.html", {
                "request": request,
                "user": db_user,
                "error": "Current password is incorrect"
            })

        # Update user's hashed password
        db_user.hashed_password = hash_password(new_password)
        db.commit()

        # Apply features to user object for rendering
        db_user = apply_plan_features(db_user)

        # Send email notification using global email_service
        try:
            email_service.send_account_changed_email(
                db_user,
                change_details="Your password was changed successfully.",
                ip_address=ip_address
            )
            print("✅ Password change notification email sent")
        except Exception as e:
            print(f"❌ Failed to send password change notification: {str(e)}")

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
    new_email = sanitize_input(new_email)
    db = SessionLocal()
    try:
        # Get IP address from request  
        ip_address = get_real_client_ip(request) if request.client else "Unknown"
        
        db_user = db.query(User).filter(User.id == user.id).first()

        # Check if new email is the same as current
        if db_user.email == new_email:
            db_user.features = get_plan_features(db_user.plan)
            return templates.TemplateResponse("account.html", {
                "request": request,
                "user": db_user,
                "error": "This is already your current email address"
            })

        # Check if new email is already in use by another user
        if db.query(User).filter(User.email == new_email, User.id != user.id).first():
            db_user.features = get_plan_features(db_user.plan)
            return templates.TemplateResponse("account.html", {
                "request": request,
                "user": db_user,
                "error": "Email already in use by another account"
            })

        # Create email change request instead of immediately changing
        change_request = create_email_change_request(user.id, new_email, db)
        
        db_user.features = get_plan_features(db_user.plan)
        
        # Send verification email to NEW address
        try:
            email_service.send_email_change_verification(
                db_user, 
                new_email, 
                change_request.token
            )
            success_message = f"Verification email sent to {new_email}. Please check your inbox and click the verification link to complete the email change."
            print("✅ Email change verification sent to new address")
        except Exception as e:
            print(f"❌ Failed to send email change verification: {str(e)}")
            success_message = f"Verification email sent to {new_email}. Please check your inbox to complete the change."

        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": db_user,
            "success": success_message
        })
    
    finally:
        db.close()

@app.post("/account/delete")
async def delete_account(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        
        # Calculate stats for goodbye email
        total_tweets = db.query(GeneratedTweet).filter(GeneratedTweet.user_id == user.id).count()
        days_active = (datetime.utcnow() - db_user.created_at).days

        # Determine which plan to show in the email
        if db_user.plan == "canceling":
            # Use the original_plan if user was in canceling state
            plan_for_email = db_user.original_plan if db_user.original_plan else "free"
        else:
            # Use current plan if not canceling
            plan_for_email = db_user.plan
        
        print(f"📧 Using plan for goodbye email: {plan_for_email}")
        
        # Send goodbye email before deleting
        try:
            email_service.send_goodbye_email(db_user, total_tweets, days_active, plan_for_email)
            print("✅ Goodbye email sent")
        except Exception as e:
            print(f"⚠ Failed to send goodbye email: {str(e)}")
        
        # Delete user and their data in the correct order (child records first)
        # Delete all foreign key references first
        db.query(Usage).filter(Usage.user_id == user.id).delete()
        db.query(GeneratedTweet).filter(GeneratedTweet.user_id == user.id).delete()
        db.query(TeamMember).filter(TeamMember.user_id == user.id).delete()
        db.query(EmailVerification).filter(EmailVerification.user_id == user.id).delete()
        db.query(EmailChangeRequest).filter(EmailChangeRequest.user_id == user.id).delete()  # ADD THIS LINE
        db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
        
        # Now delete the user record
        db.delete(db_user)
        db.commit()
        
         # Create redirect response
        response = RedirectResponse(
            "/?success=Account+deleted+successfully", 
            status_code=303  # ✅ Use 303 for POST-redirect-GET
        )
        
        # Clear the access token cookie properly
        response.delete_cookie(
            key="access_token",
            path="/",
            secure=True,
            httponly=True,
            samesite="lax"
        )
        
        return response
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error deleting account: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Redirect to account page with error instead of rendering template
        # (safer because user object might be in bad state)
        return RedirectResponse(
            "/account?error=Failed+to+delete+account", 
            status_code=303
        )
    finally:
        db.close()
        
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
        
        filename = f"tweets_export_{user.username}_{datetime.utcnow().strftime('%Y%m%d')}.json"
        
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
    # Don't redirect logged-in users anymore - let them use playground too
    return templates.TemplateResponse("tweetgiver.html", {
        "request": request, 
        "tweets": None, 
        "user": user
    })

@app.post("/tweetgiver", response_class=HTMLResponse)
async def generate_tweetgiver(request: Request):
    user = get_optional_user(request)
    
    # If logged in, redirect to dashboard
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    
    # Check playground usage count
    playground_count = int(request.cookies.get("playground_count", "0"))
    
    if playground_count >= 5:
        # Redirect to registration after 5 tweets
        return RedirectResponse("/register?message=You've reached the free limit! Sign up for unlimited tweets.", status_code=302)
    
    form = await request.form()
    job = form.get("job", "").strip()
    goal = form.get("goal", "").strip()
    
    if not job or not goal:
        return templates.TemplateResponse("tweetgiver.html", {
            "request": request, 
            "tweets": None, 
            "user": user,
            "error": "Please fill in both fields",
            "tweets_remaining": 5 - playground_count
        })
    
    # Generate single tweet for playground
    prompt = f"As a {job}, suggest 1 engaging tweet to achieve: {goal}."
    tweets = await get_ai_tweets(prompt, 1)
    
    # Increment counter
    new_count = playground_count + 1
    
    response = templates.TemplateResponse("tweetgiver.html", {
        "request": request, 
        "tweets": tweets, 
        "user": user,
        "tweets_remaining": 5 - new_count,
        "show_signup_prompt": new_count >= 3  # Show after 3 tweets
    })
    
    # Update cookie
    response.set_cookie(
        key="playground_count",
        value=str(new_count),
        max_age=86400,  # 24 hours
        httponly=True
    )
    
    return response

@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("pricing.html", {"request": request, "user": user})

@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("privacy.html", {"request": request, "user": user})

@app.get("/terms", response_class=HTMLResponse)
def terms_of_service(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("terms.html", {"request": request, "user": user})

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
        db_user = db.query(User).filter(User.id == user.id).first()
        
        # Store the original plan BEFORE marking as canceling
        if db_user.plan != "canceling" and db_user.plan != "free":
            db_user.original_plan = db_user.plan  # Save the actual plan
            print(f"📋 Storing original plan: {db_user.plan}")
        
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

        subscription = subscriptions.data[0]
        
        # ✅ CANCEL THE SUBSCRIPTION AT PERIOD END (NOT IMMEDIATELY)
        try:
            stripe.Subscription.modify(
                subscription.id,
                cancel_at_period_end=True
            )
            print(f"✅ Subscription {subscription.id} set to cancel at period end")
        except stripe.error.StripeError as e:
            return RedirectResponse(
                f"/account?error=Failed+to+cancel+subscription%3A+{str(e).replace(' ', '+')}",
                status_code=302
            )
        
        # Get cancellation date from the subscription
        cancellation_date = None
        try:
            # Try multiple ways to access the period end
            period_end = None
            
            if hasattr(subscription, 'current_period_end'):
                period_end = subscription.current_period_end
            elif 'current_period_end' in subscription:
                period_end = subscription['current_period_end']
            elif hasattr(subscription, 'get'):
                period_end = subscription.get('current_period_end')
            
            # Convert to datetime if we got a valid period_end
            if period_end:
                cancellation_date = datetime.fromtimestamp(period_end)
                print(f"📅 Cancellation date: {cancellation_date.strftime('%Y-%m-%d')}")
            else:
                print("⚠️ Could not find current_period_end in subscription")
                cancellation_date = datetime.now() + timedelta(days=30)
                print(f"📅 Using fallback cancellation date: {cancellation_date.strftime('%Y-%m-%d')}")
            
        except (KeyError, AttributeError, IndexError, TypeError, Exception) as e:
            print(f"⚠️ Could not get cancellation date: {str(e)}")
            cancellation_date = datetime.now() + timedelta(days=30)
            print(f"📅 Using fallback cancellation date: {cancellation_date.strftime('%Y-%m-%d')}")
        
        # ✅ Store cancellation date in database
        db_user.cancellation_date = cancellation_date
        db_user.cancel_at_period_end = True
        db.commit()
        
        # Send cancellation email
        try:
            # Use the original plan for the email, not "canceling"
            plan_for_email = db_user.original_plan if db_user.original_plan else "free"
            email_service.send_subscription_cancellation_email(
                db_user, 
                plan_for_email, 
                cancellation_date
            )
            print("✅ Cancellation email sent")
        except Exception as e:
            print(f"❌ Failed to send cancellation email: {str(e)}")
        
        # ✅ Don't add background task - use Stripe webhook instead
        # The webhook will handle the actual downgrade when subscription ends
        
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
        print(f"❌ System error during cancellation: {str(e)}")
        import traceback
        traceback.print_exc()
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
                    user.cancellation_date = subscriptions.current_period_end  # from Stripe
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
@app.post("/checkout/{plan_type}")
async def create_checkout_session(request: Request, plan_type: str):
    # Use get_optional_user instead of try/except
    user = get_optional_user(request)
    
    if not user:
        return RedirectResponse("/register", status_code=302)
    
    db = SessionLocal()
    try:
        # Only allow creator plan for now - others are coming soon
        if plan_type in ["small_team", "agency", "enterprise"]:
            return templates.TemplateResponse("pricing.html", {
                "request": request,
                "error": f"{plan_type.replace('_', ' ').title()} plan is coming soon! Currently only Creator plan is available.",
                "user": user
            })
        
        # Map plan types to price IDs (only creator is active)
        price_map = {
            "creator": STRIPE_CREATOR_PRICE_ID,
        }
        
        price_id = price_map.get(plan_type)
        if not price_id:
            return templates.TemplateResponse("pricing.html", {
                "request": request,
                "error": "This plan is not available yet. Coming soon!",
                "user": user
            })
        
        # Create or retrieve Stripe customer
        customer_id = None
        
        # Check if user already has a stripe_customer_id in database
        db_user = db.query(User).filter(User.id == user.id).first()
        
        if db_user.stripe_customer_id:
            # User already has a customer ID, verify it exists in Stripe
            try:
                customer = stripe.Customer.retrieve(db_user.stripe_customer_id)
                customer_id = customer.id
                print(f"✅ Using existing customer {customer_id} for user {user.email}")
            except stripe.error.InvalidRequestError:
                # Customer doesn't exist in Stripe anymore, create a new one
                print(f"⚠️ Customer {db_user.stripe_customer_id} not found in Stripe, creating new one")
                db_user.stripe_customer_id = None
                db.commit()
        
        if not customer_id:
            # Create new Stripe customer
            try:
                customer = stripe.Customer.create(
                    email=user.email,
                    name=getattr(user, 'name', user.username) if hasattr(user, 'name') else user.username,
                    metadata={
                        "user_id": str(user.id),
                        "created_from": "checkout"
                    }
                )
                customer_id = customer.id
                
                # Save the customer ID to the database user
                db_user.stripe_customer_id = customer_id
                db.commit()
                print(f"✅ Created new customer {customer_id} for user {user.email}")
                
            except Exception as e:
                db.rollback()
                print(f"❌ Failed to create Stripe customer: {str(e)}")
                import traceback
                traceback.print_exc()
                return templates.TemplateResponse("pricing.html", {
                    "request": request,
                    "error": "Failed to create customer. Please try again.",
                    "user": user
                })
        
        # Create checkout session with the customer ID
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=str(request.url_for('checkout_success')) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=str(request.url_for('pricing')),
            customer=customer_id,
            client_reference_id=str(user.id),
            metadata={
                "user_id": str(user.id), 
                "plan": plan_type
            }
        )
        
        return RedirectResponse(checkout_session.url, status_code=303)
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating checkout session: {str(e)}")
        import traceback
        traceback.print_exc()
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": "Error creating checkout session. Please try again.",
            "user": user
        })
    finally:
        db.close()

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
                generated_at=datetime.utcnow()
            )
            db.add(generated_tweet)
            db.commit()
            
            return {"tweet": tweets[0]}
        
        logger.error(f"Database error: {str(e)}")
        return {"error": "An error occurred. Please try again."}
    finally:
        db.close()

# Update success handler to change user's plan
@app.get("/checkout/success")
async def checkout_success(request: Request, session_id: str = None):
    print(f"🔍 Checkout success called with session_id: {session_id}")
    
    if not session_id or session_id == '{CHECKOUT_SESSION_ID}':
        print("❌ No valid session_id provided")
        return RedirectResponse("/pricing", status_code=302)

    try:
        print(f"📋 Retrieving Stripe session: {session_id}")
        session = stripe.checkout.Session.retrieve(session_id)
        print(f"✅ Session retrieved: {session.payment_status}")
        
        plan_name = session.metadata.get("plan", "Unknown Plan")
        print(f"📦 Plan from metadata: {plan_name}")
        
        # Check if payment was successful
        if session.payment_status != 'paid':
            print(f"⚠️ Payment not completed: {session.payment_status}")
            return RedirectResponse("/pricing?error=Payment+not+completed", status_code=302)
        
        # THIS IS THE KEY FIX - Get user_id from Stripe
        user_id = None
        if session.client_reference_id:
            user_id = int(session.client_reference_id)
            print(f"✅ Found user ID from client_reference_id: {user_id}")
        elif session.metadata.get("user_id"):
            user_id = int(session.metadata.get("user_id"))
            print(f"✅ Found user ID from metadata: {user_id}")
        else:
            print("❌ No user ID found in session")
            return RedirectResponse("/pricing?error=User+not+found", status_code=302)
        
        # Get subscription details with proper error handling
        subscription_id = session.subscription
        print(f"🔗 Subscription ID: {subscription_id}")
        
        next_billing_date = None
        amount = 0
        
        if subscription_id:
            try:
                print("📋 Retrieving subscription details...")
                subscription = stripe.Subscription.retrieve(subscription_id)
                
                print(f"🔍 Subscription status: {subscription.status}")
                
                # Get next billing date safely
                if hasattr(subscription, 'current_period_end') and subscription.current_period_end:
                    next_billing_timestamp = subscription.current_period_end
                    next_billing_date = datetime.fromtimestamp(next_billing_timestamp)
                    print(f"✅ Next billing date: {next_billing_date}")
                else:
                    next_billing_date = datetime.now() + timedelta(days=30)
                    print(f"⚠️ Using fallback billing date: {next_billing_date}")
                
                # Get amount safely
                try:
                    items_data = None
                    if hasattr(subscription, 'items'):
                        if hasattr(subscription.items, 'data'):
                            items_data = subscription.items.data
                        elif hasattr(subscription.items, '__iter__'):
                            items_data = list(subscription.items)
                        elif isinstance(subscription.items, dict) and 'data' in subscription.items:
                            items_data = subscription.items['data']
                    
                    if items_data and len(items_data) > 0:
                        price_obj = items_data[0].price
                        if hasattr(price_obj, 'unit_amount') and price_obj.unit_amount:
                            amount = price_obj.unit_amount / 100
                            print(f"💰 Amount: ${amount}")
                        else:
                            plan_amounts = {"creator": 9.0, "small_team": 79.0, "agency": 199.0, "enterprise": 499.0}
                            amount = plan_amounts.get(plan_name, 9.0)
                    else:
                        plan_amounts = {"creator": 9.0, "small_team": 79.0, "agency": 199.0, "enterprise": 499.0}
                        amount = plan_amounts.get(plan_name, 9.0)
                        
                except Exception as items_error:
                    print(f"⚠️ Error accessing items: {str(items_error)}")
                    plan_amounts = {"creator": 9.0, "small_team": 79.0, "agency": 199.0, "enterprise": 499.0}
                    amount = plan_amounts.get(plan_name, 9.0)
                    print(f"💰 Using fallback amount: ${amount}")
                    
            except Exception as sub_error:
                print(f"❌ Error retrieving subscription: {str(sub_error)}")
                import traceback
                traceback.print_exc()
                next_billing_date = datetime.now() + timedelta(days=30)
                plan_amounts = {"creator": 9.0, "small_team": 79.0, "agency": 199.0, "enterprise": 499.0}
                amount = plan_amounts.get(plan_name, 9.0)
        else:
            print("❌ No subscription found in session")
            next_billing_date = datetime.now() + timedelta(days=30)
            plan_amounts = {"creator": 9.0, "small_team": 79.0, "agency": 199.0, "enterprise": 499.0}
            amount = plan_amounts.get(plan_name, 9.0)
        
        # Update user's plan in database - USE user_id FROM STRIPE
        db = SessionLocal()
        db_user = None  # Initialize
        try:
            # Query database using user_id from Stripe (not from session cookie!)
            db_user = db.query(User).filter(User.id == user_id).first()
            
            if db_user:
                old_plan = db_user.plan
                print(f"📋 Updating user {db_user.username} (ID: {user_id}) from {old_plan} to {plan_name}")
                
                # Update plan and subscription details
                db_user.plan = plan_name
                db_user.stripe_subscription_id = subscription_id
                db_user.subscription_status = 'active'
                
                if next_billing_date:
                    db_user.subscription_end_date = next_billing_date
                
                # Get customer ID from session
                if session.customer:
                    db_user.stripe_customer_id = session.customer
                    print(f"🆔 Updated customer ID: {session.customer}")
                
                db.commit()
                print("✅ Database updated successfully")
                
                # Send upgrade email
                if next_billing_date and amount > 0:
                    try:
                        print("📧 Attempting to send upgrade email...")
                        await email_service.send_subscription_upgrade_email(
                            user=db_user,
                            old_plan=old_plan,
                            new_plan=plan_name,
                            amount=amount,
                            next_billing_date=next_billing_date
                        )
                        print("✅ Upgrade email sent successfully")
                    except Exception as e:
                        print(f"❌ Failed to send upgrade email: {str(e)}")
                        import traceback
                        traceback.print_exc()
                else:
                    print("⚠️ Skipping email - missing billing date or amount")
            else:
                print(f"❌ User not found in database with ID: {user_id}")
                        
        except Exception as db_error:
            print(f"❌ Database error: {str(db_error)}")
            db.rollback()
            import traceback
            traceback.print_exc()
        finally:
            db.close()
            
    except Exception as e:
        print(f"❌ Error processing checkout: {str(e)}")
        import traceback
        traceback.print_exc()

    # Get proper display name
    plan_display_names = {
        "creator": "Creator Plan",
        "small_team": "Small Team Plan", 
        "agency": "Agency Plan",
        "enterprise": "Enterprise Plan"
    }
    
    display_name = plan_display_names.get(plan_name, 
        plan_name.replace("_", " ").title() + " Plan")

    # For display, try to get user from session, but fall back to db_user
    current_user = get_optional_user(request) or db_user

    return templates.TemplateResponse("checkout_success.html", {
        "request": request,
        "user": current_user,
        "plan": display_name
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

@app.get("/complete-onboarding", response_class=HTMLResponse)
def complete_onboarding_get(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("onboarding.html", {"request": request, "user": user})

@app.get("/dashboard", response_class=HTMLResponse)
def user_dashboard(
    request: Request,
    success: str = None,
    error: str = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    csrf_protect: CsrfProtect = Depends()
):
    """User dashboard - For regular users (NOT admin)"""
    
    # Generate CSRF token ONCE
    csrf_response = csrf_protect.generate_csrf()
    csrf_token = csrf_response[0] if isinstance(csrf_response, tuple) else csrf_response
    
    # Ensure user has features
    current_user = apply_plan_features(current_user)
    
    # Get user's usage for today
    today = str(date.today())
    usage = db.query(Usage).filter(Usage.user_id == current_user.id, Usage.date == today).first()
    if not usage:
        usage = Usage(user_id=current_user.id, date=today, count=0)
        db.add(usage)
        db.commit()
    
    # Calculate tweets left properly handling unlimited
    daily_limit = current_user.features["daily_limit"]
    if daily_limit == float('inf'):
        tweets_left = "Unlimited"
        tweets_used = usage.count
    else:
        tweets_left = max(0, daily_limit - usage.count)
        tweets_used = usage.count
    
    # Get recent tweets
    recent_tweets = db.query(GeneratedTweet).filter(
        GeneratedTweet.user_id == current_user.id
    ).order_by(GeneratedTweet.generated_at.desc()).limit(10).all()
    
    response = templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": current_user,
        "features": current_user.features,
        "tweets_left": tweets_left,
        "tweets_used": tweets_used,
        "recent_tweets": recent_tweets,
        "tweets": [],
        "success": success,
        "error": error,
        "csrf_token": csrf_token,
        "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
    })
    
    # Set CSRF cookie with dynamic secure flag
    response.set_cookie(
        key="fastapi-csrf-token",
        value=csrf_token,
        max_age=3600,
        path="/",
        secure=IS_PRODUCTION,
        httponly=True,
        samesite="lax"
    )
    
    return response

@limiter.limit("30/hour")
@app.post("/dashboard", response_class=HTMLResponse)
async def generate(request: Request, csrf_protect: CsrfProtect = Depends()):
    db = SessionLocal()
    
    try:
        # Get user first
        user = get_current_user(request)
        user = apply_plan_features(user)
        
        # Get form data
        form = await request.form()
        
        # ✅ VALIDATE CSRF MANUALLY (more reliable)
        csrf_token_from_form = form.get("csrf_token")
        csrf_token_from_cookie = request.cookies.get("fastapi-csrf-token")
        
        print(f"Form CSRF token: {csrf_token_from_form}")
        print(f"Cookie CSRF token: {csrf_token_from_cookie}")
        
        if not csrf_token_from_cookie or not csrf_token_from_form:
            print("CSRF validation failed: Missing token")
            
        if not csrf_token_from_cookie or not csrf_token_from_form:
            print("CSRF validation failed: Missing token")
            
            existing_token = csrf_token_from_cookie or csrf_token_from_form or csrf_protect.generate_csrf()
            if isinstance(existing_token, tuple):
                existing_token = existing_token[0]
            
            # Get usage info
            today = str(date.today())
            usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
            
            daily_limit = user.features["daily_limit"]
            if daily_limit == float('inf'):
                tweets_left = "Unlimited"
            else:
                tweets_left = max(0, daily_limit - (usage.count if usage else 0))
            
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "features": user.features,
                "tweets_left": tweets_left,
                "tweets_used": usage.count if usage else 0,
                "tweets": [],
                "error": "Invalid CSRF token. Please refresh and try again.",
                "csrf_token": existing_token,
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })
        
        if csrf_token_from_cookie != csrf_token_from_form:
            print(f"CSRF validation failed: Token mismatch")
            print(f"Expected: {csrf_token_from_cookie}")
            print(f"Got: {csrf_token_from_form}")
            
            # Get existing token for reuse
            existing_token = request.cookies.get("fastapi-csrf-token")
            
            # Get usage info
            today = str(date.today())
            usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
            
            daily_limit = user.features["daily_limit"]
            if daily_limit == float('inf'):
                tweets_left = "Unlimited"
            else:
                tweets_left = max(0, daily_limit - (usage.count if usage else 0))
            
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "features": user.features,
                "tweets_left": tweets_left,
                "tweets_used": usage.count if usage else 0,
                "tweets": [],
                "error": "Invalid CSRF token. Please refresh and try again.",
                "csrf_token": existing_token,
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })

        # Get form fields
        job = form.get("job")
        goal = form.get("goal")
        tweet_count_str = form.get("tweet_count", "1")
        
        try:
            tweet_count = int(tweet_count_str)
        except ValueError:
            tweet_count = 1

        # Verify reCAPTCHA
        g_recaptcha_response = form.get("g-recaptcha-response", "")
        if not verify_recaptcha(g_recaptcha_response):
            existing_token = request.cookies.get("fastapi-csrf-token")
            
            today = str(date.today())
            usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
            
            daily_limit = user.features["daily_limit"]
            if daily_limit == float('inf'):
                tweets_left = "Unlimited"
            else:
                tweets_left = max(0, daily_limit - (usage.count if usage else 0))
            
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "features": user.features,
                "tweets_left": tweets_left,
                "tweets_used": usage.count if usage else 0,
                "tweets": [],
                "error": "Please complete the reCAPTCHA verification",
                "csrf_token": existing_token,
                "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
            })

        # Get usage for today
        today = str(date.today())
        usage = db.query(Usage).filter(Usage.user_id == user.id, Usage.date == today).first()
        if not usage:
            usage = Usage(user_id=user.id, date=today, count=0)
            db.add(usage)
            db.commit()

        # Calculate tweets left - proper unlimited handling
        daily_limit = user.features["daily_limit"]
        
        if daily_limit == float('inf'):
            tweets_left = "Unlimited"
            # No limit check needed for unlimited plans
        else:
            tweets_left = max(0, daily_limit - usage.count)
            
            # Only check limit for non-unlimited plans
            if tweets_left <= 0:
                existing_token = request.cookies.get("fastapi-csrf-token")
                
                return templates.TemplateResponse("dashboard.html", {
                    "request": request,
                    "user": user,
                    "features": user.features,
                    "tweets_left": 0,
                    "tweets_used": usage.count,
                    "tweets": [],
                    "error": "Daily limit reached! Upgrade for unlimited tweets.",
                    "csrf_token": existing_token,
                    "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
                })
            
            # Cap tweet count at remaining limit for non-unlimited users
            if tweet_count > tweets_left:
                tweet_count = tweets_left

        # Generate tweets
        prompt = f"As a {job}, suggest {tweet_count} engaging tweets to achieve: {goal}."
        tweets = await get_ai_tweets(prompt, count=tweet_count)

        # Save to history
        for tweet_text in tweets:
            generated_tweet = GeneratedTweet(
                user_id=user.id,
                tweet_text=tweet_text,
                generated_at=datetime.utcnow()
            )
            db.add(generated_tweet)

        # Update usage
        usage.count += len(tweets)
        db.commit()

        # Calculate remaining
        if daily_limit == float('inf'):
            new_tweets_left = "Unlimited"
        else:
            new_tweets_left = max(0, daily_limit - usage.count)

        # Use existing token
        existing_token = request.cookies.get("fastapi-csrf-token")

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "features": user.features,
            "tweets": tweets,
            "tweets_left": new_tweets_left,
            "tweets_used": usage.count,
            "error": None,
            "csrf_token": existing_token,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
        
    except Exception as e:
        print(f"Dashboard generation error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        existing_token = request.cookies.get("fastapi-csrf-token")
        
        try:
            user = get_current_user(request)
            user = apply_plan_features(user)
        except:
            user = None
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "features": user.features if user else {},
            "tweets_left": 0,
            "tweets": [],
            "error": "An error occurred. Please try again.",
            "csrf_token": existing_token,
            "recaptcha_site_key": os.getenv("RECAPTCHA_SITE_KEY")
        })
        
    finally:
        db.close()

# Fixed Email Templates with simpler CSS
EMAIL_TEMPLATES = {
    "welcome": {
        "subject": "Welcome to GiverAI! Your Twitter Content Creation Journey Starts Now 🚀",
        "body": """
        <html>
        <head>
            <style>
                body { 
                    font-family: Arial, sans-serif; 
                    line-height: 1.6; 
                    color: #333; 
                    margin: 0; 
                    padding: 0; 
                }
                .container { 
                    max-width: 600px; 
                    margin: 0 auto; 
                    padding: 20px; 
                }
                .header { 
                    background: #667eea; 
                    color: white; 
                    padding: 30px; 
                    text-align: center; 
                    border-radius: 8px 8px 0 0; 
                }
                .content { 
                    background: white; 
                    padding: 30px; 
                    border-radius: 0 0 8px 8px; 
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
                }
                .button { 
                    display: inline-block; 
                    background: #667eea; 
                    color: white; 
                    padding: 12px 24px; 
                    text-decoration: none; 
                    border-radius: 6px; 
                    margin: 15px 0; 
                }
                .feature-box { 
                    background: #f8f9fa; 
                    padding: 15px; 
                    margin: 10px 0; 
                    border-left: 4px solid #667eea; 
                    border-radius: 4px; 
                }
                .footer { 
                    text-align: center; 
                    margin-top: 30px; 
                    color: #666; 
                    font-size: 12px; 
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Welcome to GiverAI! 🎉</h1>
                    <p>Your AI-powered Twitter content creation platform</p>
                </div>
                <div class="content">
                    <h2>Hi {username}! 👋</h2>
                    <p>We're thrilled to have you join our community of content creators who are transforming their Twitter presence with AI.</p>
                    
                    <div class="feature-box">
                        <h3>🚀 What's Next?</h3>
                        <p>Complete your profile setup and start generating engaging tweets tailored to your industry and goals.</p>
                    </div>
                    
                    <div class="feature-box">
                        <h3>✨ Your Free Plan Includes:</h3>
                        <ul>
                            <li>15 AI-generated tweets per day</li>
                            <li>Basic customization options</li>
                            <li>1-day tweet history</li>
                        </ul>
                    </div>
                    
                    <p>Ready to create your first viral tweet?</p>
                    <a href="{dashboard_url}" class="button">Start Creating Tweets</a>
                    
                    <p>Need help getting started? Check out our <a href="{help_url}">quick start guide</a> or reply to this email with any questions.</p>
                    
                    <p>Happy tweeting!</p>
                    <p><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>You received this email because you signed up for GiverAI.</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "verification": {
        "subject": "Verify Your GiverAI Account - Almost Ready! ✅",
        "body": """
        <html>
        <head>
            <style>
                body { 
                    font-family: Arial, sans-serif; 
                    line-height: 1.6; 
                    color: #333; 
                    margin: 0; 
                    padding: 0; 
                }
                .container { 
                    max-width: 600px; 
                    margin: 0 auto; 
                    padding: 20px; 
                }
                .header { 
                    background: #667eea; 
                    color: white; 
                    padding: 30px; 
                    text-align: center; 
                    border-radius: 8px 8px 0 0; 
                }
                .content { 
                    background: white; 
                    padding: 30px; 
                    border-radius: 0 0 8px 8px; 
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
                }
                .verify-button { 
                    display: inline-block; 
                    background: #28a745; 
                    color: white; 
                    padding: 15px 30px; 
                    text-decoration: none; 
                    border-radius: 6px; 
                    margin: 20px 0; 
                    font-size: 18px; 
                    font-weight: bold; 
                }
                .verification-code { 
                    background: #f8f9fa; 
                    padding: 20px; 
                    margin: 20px 0; 
                    text-align: center; 
                    border-radius: 8px; 
                    border: 2px dashed #667eea; 
                }
                .footer { 
                    text-align: center; 
                    margin-top: 30px; 
                    color: #666; 
                    font-size: 12px; 
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Verify Your Email 📧</h1>
                    <p>One more step to unlock GiverAI</p>
                </div>
                <div class="content">
                    <h2>Hi {username}!</h2>
                    <p>Thanks for signing up for GiverAI! To complete your registration and start creating amazing tweets, please verify your email address.</p>
                    
                    <div style="text-align: center;">
                        <a href="{verification_url}" class="verify-button">Verify Email Address</a>
                    </div>
                    
                    <div class="verification-code">
                        <p><strong>Verification Code:</strong></p>
                        <h2 style="color: #667eea; font-size: 24px; margin: 10px 0;">{verification_code}</h2>
                        <p><small>Use this code if the button above doesn't work</small></p>
                    </div>
                    
                    <p><strong>This verification link expires in 24 hours.</strong></p>
                    
                    <p>If you didn't create an account with GiverAI, please ignore this email.</p>
                    
                    <p>Best regards,<br><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This verification email was sent to {email}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "account_changed": {
        "subject": "Your GiverAI Account Was Updated 🔐",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 25px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .alert-box { background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; margin: 20px 0; border-radius: 6px; }
                .change-details { background: #f8f9fa; padding: 15px; margin: 15px 0; border-radius: 6px; }
                .button { display: inline-block; background: #dc3545; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 15px 0; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Account Security Alert 🔐</h1>
                    <p>Important changes to your GiverAI account</p>
                </div>
                <div class="content">
                    <h2>Hi {username},</h2>
                    
                    <div class="alert-box">
                        <p><strong>⚠️ Your account information was recently updated</strong></p>
                    </div>
                    
                    <div class="change-details">
                        <h3>What Changed:</h3>
                        <p>{change_details}</p>
                        <p><strong>When:</strong> {timestamp}</p>
                        <p><strong>IP Address:</strong> {ip_address}</p>
                    </div>
                    
                    <h3>Was this you?</h3>
                    <p>If you made this change, no action is needed. Your account is secure.</p>
                    
                    <p><strong>If you didn't make this change:</strong></p>
                    <ol>
                        <li>Change your password immediately</li>
                        <li>Review your account settings</li>
                        <li>Contact our support team</li>
                    </ol>
                    
                    <a href="{account_url}" class="button">Secure My Account</a>
                    
                    <p>For your security, we recommend using a strong, unique password and enabling two-factor authentication.</p>
                    
                    <p>If you need help, please contact us at support@giverai.me</p>
                    
                    <p>Stay secure,<br><strong>The GiverAI Security Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This security alert was sent to {email}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "subscription_changed": {
        "subject": "Your GiverAI Subscription Has Been Updated 💼",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .plan-box { background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 20px; margin: 20px 0; border-radius: 8px; text-align: center; }
                .feature-list { background: #f8f9fa; padding: 20px; margin: 15px 0; border-radius: 6px; }
                .button { display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 15px 0; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Subscription Updated! 🎉</h1>
                    <p>Your GiverAI plan has changed</p>
                </div>
                <div class="content">
                    <h2>Hi {username}!</h2>
                    
                    <div class="plan-box">
                        <h2>🚀 Welcome to {new_plan}!</h2>
                        <p>{plan_description}</p>
                    </div>
                    
                    <h3>What's New in Your Plan:</h3>
                    <div class="feature-list">
                        {new_features}
                    </div>
                    
                    <h3>Billing Details:</h3>
                    <ul>
                        <li><strong>Plan:</strong> {new_plan}</li>
                        <li><strong>Next Billing Date:</strong> {next_billing_date}</li>
                        <li><strong>Amount:</strong> {amount}</li>
                    </ul>
                    
                    <p>Your new features are active immediately. Start exploring them now!</p>
                    
                    <a href="{dashboard_url}" class="button">Explore New Features</a>
                    
                    <p>Questions about your subscription? Check our <a href="{billing_url}">billing FAQ</a> or contact support.</p>
                    
                    <p>Thanks for choosing GiverAI!</p>
                    <p><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>Manage your subscription: {billing_url}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "team_invitation": {
        "subject": "You've Been Invited to Join {team_owner}'s GiverAI Team! 👥",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .invitation-box { background: #e3f2fd; padding: 20px; margin: 20px 0; border-radius: 8px; border-left: 4px solid #2196f3; }
                .accept-button { display: inline-block; background: #28a745; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px; margin: 15px 10px 15px 0; font-weight: bold; }
                .decline-button { display: inline-block; background: #6c757d; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px; margin: 15px 0; font-weight: bold; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Team Invitation 📨</h1>
                    <p>You've been invited to collaborate!</p>
                </div>
                <div class="content">
                    <h2>Hi there! 👋</h2>
                    
                    <div class="invitation-box">
                        <h3>🎉 You're Invited!</h3>
                        <p><strong>{team_owner}</strong> has invited you to join their GiverAI team as a <strong>{role}</strong>.</p>
                    </div>
                    
                    <h3>What This Means:</h3>
                    <ul>
                        <li>✅ Access to the team's GiverAI workspace</li>
                        <li>✅ Collaborate on tweet generation and content strategy</li>
                        <li>✅ Share and manage team-generated content</li>
                        <li>✅ {role_permissions}</li>
                    </ul>
                    
                    <h3>About the Team:</h3>
                    <p><strong>Team:</strong> {team_name}<br>
                    <strong>Plan:</strong> {team_plan}<br>
                    <strong>Industry:</strong> {team_industry}</p>
                    
                    <p>Ready to start collaborating on amazing Twitter content?</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{accept_url}" class="accept-button">Accept Invitation</a>
                        <a href="{decline_url}" class="decline-button">Decline</a>
                    </div>
                    
                    <p><small>This invitation expires in 7 days. If you don't have a GiverAI account, you'll be prompted to create one.</small></p>
                    
                    <p>Questions? Contact {team_owner} or our support team at support@giverai.me</p>
                    
                    <p>Looking forward to having you on the team!</p>
                    <p><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This invitation was sent to {email} by {team_owner}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },

    "forgot_password": {
        "subject": "Reset Your GiverAI Password 🔐",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: #667eea; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .reset-button { display: inline-block; background: #28a745; color: white; padding: 15px 30px; text-decoration: none; border-radius: 6px; margin: 20px 0; font-size: 18px; font-weight: bold; }
                .security-notice { background: #fff3cd; padding: 15px; margin: 20px 0; border-radius: 6px; border: 1px solid #ffeaa7; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Password Reset Request 🔐</h1>
                    <p>Reset your GiverAI password</p>
                </div>
                <div class="content">
                    <h2>Hi {username}!</h2>
                    <p>We received a request to reset your password for your GiverAI account.</p>
                    
                    <div style="text-align: center;">
                        <a href="{reset_url}" class="reset-button">Reset My Password</a>
                    </div>
                    
                    <div class="security-notice">
                        <h3>🛡️ Security Information:</h3>
                        <ul>
                            <li>This link expires in <strong>1 hour</strong></li>
                            <li>Request made from IP: <strong>{ip_address}</strong></li>
                            <li>If you didn't request this, please ignore this email</li>
                        </ul>
                    </div>
                    
                    <p>If the button above doesn't work, copy and paste this link into your browser:</p>
                    <p style="word-break: break-all; background: #f8f9fa; padding: 10px; border-radius: 4px;">
                        {reset_url}
                    </p>
                    
                    <p>If you didn't request a password reset, your account is still secure and no action is needed.</p>
                    
                    <p>Need help? Contact us at support@giverai.me</p>
                    
                    <p>Best regards,<br><strong>The GiverAI Security Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This password reset email was sent to {email}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "forgot_username": {
        "subject": "Your GiverAI Username Reminder 👤",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: #667eea; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .username-box { background: #e3f2fd; padding: 20px; margin: 20px 0; text-align: center; border-radius: 8px; border: 2px solid #2196f3; }
                .login-button { display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 15px 0; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Username Reminder 👤</h1>
                    <p>Here's your GiverAI username</p>
                </div>
                <div class="content">
                    <h2>Hi there!</h2>
                    <p>You requested a reminder of your GiverAI username. Here it is:</p>
                    
                    <div class="username-box">
                        <h3>Your Username:</h3>
                        <h2 style="color: #667eea; font-size: 24px; margin: 10px 0;">{username}</h2>
                    </div>
                    
                    <p>Now you can log in to your GiverAI account:</p>
                    <div style="text-align: center;">
                        <a href="{login_url}" class="login-button">Log In to GiverAI</a>
                    </div>
                    
                    <p><strong>Account Details:</strong></p>
                    <ul>
                        <li>Username: <strong>{username}</strong></li>
                        <li>Email: <strong>{email}</strong></li>
                        <li>Plan: <strong>{plan}</strong></li>
                        <li>Member since: <strong>{member_since}</strong></li>
                    </ul>
                    
                    <p>If you also forgot your password, you can <a href="{forgot_password_url}">reset it here</a>.</p>
                    
                    <p>If you didn't request this reminder, please ignore this email.</p>
                    
                    <p>Happy tweeting!</p>
                    <p><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This username reminder was sent to {email}</p>
                </div>
            </div>
        </body>
        </html>
        """
    },
    
    "password_reset_success": {
        "subject": "Your GiverAI Password Has Been Changed ✅",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: #28a745; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .success-box { background: #d4edda; padding: 15px; margin: 20px 0; border-radius: 6px; border: 1px solid #c3e6cb; }
                .security-tips { background: #f8f9fa; padding: 20px; margin: 20px 0; border-radius: 6px; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Password Changed Successfully! ✅</h1>
                    <p>Your GiverAI account is secure</p>
                </div>
                <div class="content">
                    <h2>Hi {username}!</h2>
                    
                    <div class="success-box">
                        <h3>🎉 Password Updated!</h3>
                        <p>Your GiverAI password has been successfully changed.</p>
                        <p><strong>When:</strong> {timestamp}</p>
                        <p><strong>IP Address:</strong> {ip_address}</p>
                    </div>
                    
                    <div class="security-tips">
                        <h3>🛡️ Security Tips:</h3>
                        <ul>
                            <li>Keep your password private and secure</li>
                            <li>Use a unique password for your GiverAI account</li>
                            <li>Consider using a password manager</li>
                            <li>Log out from shared or public computers</li>
                        </ul>
                    </div>
                    
                    <p>If you didn't change your password, please contact our support team immediately at support@giverai.me</p>
                    
                    <p>Your account security is our priority. Thank you for keeping your account safe!</p>
                    
                    <p>Best regards,<br><strong>The GiverAI Security Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This notification was sent to {email}</p>
                </div>
            </div>
        </body>
        </html>
        """
          },
    
    "goodbye": {
        "subject": "We're Sorry to See You Go - Your GiverAI Account Has Been Deleted 👋",
        "body": """
        <html>
        <head>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }
                .container { max-width: 600px; margin: 0 auto; padding: 20px; }
                .header { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }
                .content { background: white; padding: 30px; border-radius: 0 0 8px 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                .data-box { background: #fff3cd; padding: 15px; margin: 20px 0; border-radius: 6px; border: 1px solid #ffeaa7; }
                .feedback-box { background: #e3f2fd; padding: 20px; margin: 20px 0; border-radius: 8px; }
                .button { display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 15px 0; }
                .footer { text-align: center; margin-top: 30px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Account Deleted 😢</h1>
                    <p>We're sorry to see you go, {username}</p>
                </div>
                <div class="content">
                    <h2>Your GiverAI account has been successfully deleted</h2>
                    
                    <div class="data-box">
                        <h3>🗑️ Data Removal</h3>
                        <p>As requested, we have permanently deleted:</p>
                        <ul>
                            <li>Your account profile and settings</li>
                            <li>All generated tweets and content history</li>
                            <li>Usage data and analytics</li>
                            <li>Team memberships and collaborations</li>
                            <li>Billing and subscription information</li>
                        </ul>
                        <p><strong>This action cannot be undone.</strong></p>
                    </div>
                    
                    <h3>Thank You for Using GiverAI</h3>
                    <p>We appreciate the time you spent with us. During your journey, you:</p>
                    <ul>
                        <li>📝 Generated {total_tweets} tweets</li>
                        <li>📅 Were with us for {days_active} days</li>
                        <li>🎯 Used the {plan_name} plan</li>
                    </ul>
                    
                    <div class="feedback-box">
                        <h3>💬 Help Us Improve</h3>
                        <p>We'd love to hear why you decided to leave. Your feedback helps us make GiverAI better for everyone.</p>
                        <a href="{feedback_url}" class="button">Share Feedback</a>
                        <p><small>This survey takes less than 2 minutes and is completely anonymous.</small></p>
                    </div>
                    
                    <h3>Changed Your Mind?</h3>
                    <p>You're always welcome back! If you decide to return, you can create a new account anytime, though your previous data cannot be restored.</p>
                    
                    <a href="{signup_url}" class="button">Return to GiverAI</a>
                    
                    <p>We hope our paths cross again in the future. Until then, we wish you all the best with your content creation journey!</p>
                    
                    <p>Farewell and best wishes,</p>
                    <p><strong>The GiverAI Team</strong></p>
                </div>
                <div class="footer">
                    <p>GiverAI - AI-Powered Twitter Content Creation</p>
                    <p>This confirmation was sent to {email}</p>
                    <p>You will not receive any further emails from us.</p>
                </div>
            </div>
        </body>
        </html>
        """
    }
}

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

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    db = SessionLocal()
    try:
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")
        
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET")
            )
        except ValueError:
            return JSONResponse(content={"error": "Invalid payload"}, status_code=400)
        except stripe.error.SignatureVerificationError:
            return JSONResponse(content={"error": "Invalid signature"}, status_code=400)
        
        # Handle subscription updated (when user cancels)
        if event["type"] == "customer.subscription.updated":
            subscription = event["data"]["object"]
            customer_id = subscription["customer"]
            cancel_at_period_end = subscription.get("cancel_at_period_end", False)
            
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                if cancel_at_period_end:
                    # User canceled - mark as canceling
                    if not user.cancel_at_period_end:
                        print(f"🔔 User {user.id} canceled subscription")
                        if user.plan and user.plan not in ["free", "canceling"]:
                            user.original_plan = user.plan
                        else:
                            user.original_plan = None
                        user.cancel_at_period_end = True
                        
                        # Store cancellation date
                        period_end = subscription.get("current_period_end")
                        if period_end:
                            user.cancellation_date = datetime.fromtimestamp(period_end)
                        
                        db.commit()

                try:
                    plan_for_email = user.original_plan if user.original_plan else user.plan
                    email_service.send_subscription_cancellation_email(
                        user, 
                        plan_for_email, 
                        user.cancellation_date
                    )
                    print(f"✅ Cancellation email sent to {user.email}")
                except Exception as e:
                    print(f"❌ Failed to send cancellation email: {str(e)}")

                else:
                    # User reactivated - restore plan
                    if user.plan == "canceling" and user.original_plan:
                        print(f"🔄 User {user.id} reactivated subscription")
                        user.plan = user.original_plan
                        user.original_plan = None
                        user.cancellation_date = None
                        db.commit()
        
        # Handle subscription deleted/ended event
        elif event["type"] == "customer.subscription.deleted":
            subscription = event["data"]["object"]
            customer_id = subscription["customer"]
            
            # Find user and downgrade to free
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                print(f"🔽 Downgrading user {user.id} to free plan")
                
                user.plan = "free"
                user.cancellation_date = None
                user.cancel_at_period_end = False
                user.original_plan = None

                db.commit()
                
                # Send downgrade confirmation email
                try:
                    email_service.send_downgrade_confirmation_email(user)
                except Exception as e:
                    print(f"Failed to send downgrade email: {str(e)}")
        
        # Handle successful payment
        elif event["type"] == "invoice.payment_succeeded":
            invoice = event["data"]["object"]
            customer_id = invoice.get("customer")
            subscription_id = invoice.get("subscription")
            
            if not customer_id or not subscription_id:
                print("⚠️ Missing customer_id or subscription_id in invoice")
                return JSONResponse(content={"status": "success"}, status_code=200)
            
            try:
                # Get subscription details to determine plan
                subscription = stripe.Subscription.retrieve(subscription_id)
                
                # Safely get price_id
                if not subscription.get("items") or not subscription["items"].get("data"):
                    print("⚠️ No items in subscription")
                    return JSONResponse(content={"status": "success"}, status_code=200)
                
                price_id = subscription["items"]["data"][0]["price"]["id"]
                
                # Update user's plan based on price_id
                user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
                if user:
                    # Map price_id to plan name
                    creator_monthly = os.getenv("STRIPE_CREATOR_PRICE_MONTHLY")
                    creator_yearly = os.getenv("STRIPE_CREATOR_PRICE_YEARLY")
                    
                    if price_id == creator_monthly:
                        user.plan = "creator_monthly"
                        print(f"✅ Updated user {user.id} to creator_monthly")
                    elif price_id == creator_yearly:
                        user.plan = "creator_yearly"
                        print(f"✅ Updated user {user.id} to creator_yearly")
                    else:
                        print(f"⚠️ Unknown price_id: {price_id}")
                    
                    # Clear cancellation status if they were canceling
                    if user.original_plan and user.plan != "canceling":
                        user.original_plan = None
                        user.cancellation_date = None
                    
                    db.commit()
                else:
                    print(f"⚠️ User not found for customer_id: {customer_id}")
                    
            except stripe.error.StripeError as e:
                print(f"⚠️ Stripe error retrieving subscription: {str(e)}")
            except Exception as e:
                print(f"⚠️ Error processing payment: {str(e)}")
                import traceback
                traceback.print_exc()
        
        # Handle failed payment
        elif event["type"] == "invoice.payment_failed":
            invoice = event["data"]["object"]
            customer_id = invoice.get("customer")
            
            if not customer_id:
                print("⚠️ Missing customer_id in failed invoice")
                return JSONResponse(content={"status": "success"}, status_code=200)
            
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                print(f"⚠️ Payment failed for user {user.id}")
                
                # Send payment failed email
                try:
                    email_service.send_payment_failed_email(user)
                except Exception as e:
                    print(f"Failed to send payment failed email: {str(e)}")
            else:
                print(f"⚠️ User not found for customer_id: {customer_id}")
        
        return JSONResponse(content={"status": "success"}, status_code=200)
        
    except Exception as e:
        print(f"Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=400)
    finally:
        db.close()

def get_plan_price(plan):
    """Get plan price for emails"""
    prices = {
        'creator': 29,
        'small_team': 79,
        'agency': 199,
        'enterprise': 499
    }
    return prices.get(plan, 0)
        
def get_plan_from_price_id(price_id):
    """Map Stripe price IDs back to plan names"""
    price_to_plan = {
        STRIPE_CREATOR_PRICE_ID: "creator",
        STRIPE_SMALL_TEAM_PRICE_ID: "small_team", 
        STRIPE_AGENCY_PRICE_ID: "agency",
        STRIPE_ENTERPRISE_PRICE_ID: "enterprise"
    }
    return price_to_plan.get(price_id, "creator")
    
def get_next_billing_date(stripe_customer_id):
    """Get the next billing date for a customer's active subscription"""
    try:
        subscriptions = stripe.Subscription.list(
            customer=stripe_customer_id,
            status="active",
            limit=1
        )
        
        if subscriptions.data:
            subscription = subscriptions.data[0]
            next_billing_timestamp = subscription.current_period_end
            return datetime.fromtimestamp(next_billing_timestamp)
        
        return None
    except Exception as e:
        print(f"Error getting billing date: {str(e)}")
        return None

# Background task functions
async def handle_subscription_created(subscription):
    """Handle new subscription creation"""
    db = SessionLocal()
    try:
        customer_id = subscription['customer']
        
        # Get the user
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            print(f"❌ User not found for customer {customer_id}")
            return
            
        # Determine plan from price ID
        price_id = subscription['items']['data'][0]['price']['id']
        new_plan = get_plan_from_price_id(price_id)
        old_plan = user.plan
        new_plan = user.original_plan
        
        if new_plan and new_plan != old_plan:
            # Update user plan
            user.plan = new_plan
            db.commit()
            
            # Get billing info
            amount = subscription['items']['data'][0]['price']['unit_amount'] / 100
            
            # Debug: Check what's in current_period_end
            print(f"🔍 Subscription current_period_end: {subscription.get('current_period_end')}")
            
            # Get billing cycle end date
            period_end = subscription.get('current_period_end')
            if period_end:
                next_billing = datetime.fromtimestamp(period_end).strftime('%B %d, %Y')
            else:
                next_billing = "your next billing cycle"  # This might be your fallback
            
            print(f"🔍 Next billing formatted: {next_billing}")
            
            # Send upgrade email
            try:
                email_service.send_subscription_upgrade_email(
                    user, old_plan, new_plan, amount, next_billing
                )
                print(f"✅ Upgrade email sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to send upgrade email: {str(e)}")
                
    except Exception as e:
        print(f"❌ Error in handle_subscription_created: {str(e)}")
    finally:
        db.close()
            
async def handle_subscription_updated(subscription):
    """Handle subscription updates"""
    db = SessionLocal()
    try:
        customer_id = subscription['customer']
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        
        if not user:
            # Try to find user by email if customer ID lookup fails
            try:
                # Get customer details from Stripe
                customer = stripe.Customer.retrieve(customer_id)
                user = db.query(User).filter(User.email == customer['email']).first()
                
                if user:
                    # Update the user's stripe_customer_id
                    user.stripe_customer_id = customer_id
                    db.commit()
                    print(f"✅ Updated user {user.email} with customer ID {customer_id}")
                else:
                    print(f"❌ User not found for customer {customer_id} or email {customer.get('email', 'unknown')}")
                    return
            except Exception as e:
                print(f"❌ Could not retrieve customer or find user: {str(e)}")
                return
            
        # Check if subscription is being cancelled
        if subscription.get('cancel_at_period_end'):
            
            # Store the original plan BEFORE changing to "canceling" (only if not already canceling)
            if user.plan != "canceling":
                original_plan = user.plan  # This is the plan we want to show in emails
                
                # If you have an original_plan field in your User model, store it there
                if hasattr(user, 'original_plan'):
                    user.original_plan = user.plan
                
                # Mark as canceling
                user.plan = "canceling"
                db.commit()
            else:
                # If already canceling, try to get the original plan from stored value or fallback
                original_plan = getattr(user, 'original_plan', 'creator')
            
            # Safely get cancellation date with fallback
            current_period_end = subscription.get('current_period_end')
            if current_period_end:
                cancellation_date = datetime.fromtimestamp(current_period_end)
            else:
                cancellation_date = None
            
            try:
                email_service.send_subscription_cancellation_email(
                    user, original_plan, cancellation_date
                )
                print(f"✅ Cancellation email sent to {user.email} for {original_plan} plan")
            except Exception as e:
                print(f"❌ Failed to send cancellation email: {str(e)}")
                
    except Exception as e:
        print(f"❌ Error in handle_subscription_updated: {str(e)}")
    finally:
        db.close()
        
async def handle_subscription_created(subscription):
    """Handle new subscription creation"""
    db = SessionLocal()
    try:
        customer_id = subscription['customer']
        
        # Get the user
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            print(f"❌ User not found for customer {customer_id}")
            return
            
        # Safely get price ID and plan
        items = subscription.get('items', {}).get('data', [])
        if not items:
            print(f"❌ No subscription items found for customer {customer_id}")
            return
            
        price_id = items[0].get('price', {}).get('id')
        if not price_id:
            print(f"❌ No price ID found for customer {customer_id}")
            return
            
        new_plan = get_plan_from_price_id(price_id)
        old_plan = user.plan
        
        if new_plan and new_plan != old_plan:
            # Update user plan
            user.plan = new_plan
            db.commit()
            
            # Get billing info safely
            unit_amount = items[0].get('price', {}).get('unit_amount', 0)
            amount = unit_amount / 100 if unit_amount else 0
            
            # Safely get next billing date
            current_period_end = subscription.get('current_period_end')
            if current_period_end:
                next_billing = datetime.fromtimestamp(current_period_end).strftime('%B %d, %Y')
            else:
                next_billing = "your next billing cycle"
            
            # Send upgrade email
            try:
                email_service.send_subscription_upgrade_email(
                    user, old_plan, new_plan, amount, next_billing
                )
                print(f"✅ Upgrade email sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to send upgrade email: {str(e)}")
                
    except Exception as e:
        print(f"❌ Error in handle_subscription_created: {str(e)}")
    finally:
        db.close()

async def handle_subscription_created(subscription):
    """Handle new subscription creation"""
    db = SessionLocal()
    try:
        customer_id = subscription['customer']
        
        # Get the user
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            print(f"❌ User not found for customer {customer_id}")
            return
            
        # Safely get price ID and plan
        items = subscription.get('items', {}).get('data', [])
        if not items:
            print(f"❌ No subscription items found for customer {customer_id}")
            return
            
        price_id = items[0].get('price', {}).get('id')
        if not price_id:
            print(f"❌ No price ID found for customer {customer_id}")
            return
            
        new_plan = get_plan_from_price_id(price_id)
        old_plan = user.plan
        
        if new_plan and new_plan != old_plan:
            # Update user plan
            user.plan = new_plan
            db.commit()
            
            # Get billing info safely
            unit_amount = items[0].get('price', {}).get('unit_amount', 0)
            amount = unit_amount / 100 if unit_amount else 0
            
            # Safely get next billing date
            current_period_end = subscription.get('current_period_end')
            if current_period_end:
                next_billing = datetime.fromtimestamp(current_period_end).strftime('%B %d, %Y')
            else:
                next_billing = "your next billing cycle"
            
            # Send upgrade email
            try:
                email_service.send_subscription_upgrade_email(
                    user, old_plan, new_plan, amount, next_billing
                )
                print(f"✅ Upgrade email sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to send upgrade email: {str(e)}")
                
    except Exception as e:
        print(f"❌ Error in handle_subscription_created: {str(e)}")
    finally:
        db.close()
            
async def handle_subscription_deleted(subscription):
    """Handle subscription deletion (immediate downgrade)"""
    db = SessionLocal()
    try:
        customer_id = subscription['customer']
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        
        if user:
            old_plan = user.plan if user.plan != "canceling" else subscription.get('user_metadata', {}).get('original_plan', 'creator')
            user.plan = "free"
            db.commit()
            
            # Send downgrade notification
            try:
                email_service.send_subscription_downgrade_email(user, old_plan)
                print(f"✅ Downgrade email sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to send downgrade email: {str(e)}")
                
    except Exception as e:
        print(f"❌ Error in handle_subscription_deleted: {str(e)}")
    finally:
        db.close()
            
async def handle_first_payment_success(invoice):
    """Handle successful first payment for new subscriptions"""
    db = SessionLocal()
    try:
        customer_id = invoice['customer']
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        
        if user and user.plan != "free":
            # Additional welcome email or confirmation
            print(f"✅ First payment successful for {user.email} on {user.plan} plan")
            
    except Exception as e:
        print(f"❌ Error in handle_first_payment_success: {str(e)}")
    finally:
        db.close()
            
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
            
def get_plan_from_price_id(price_id):
    """Map Stripe price ID to plan name"""
    price_map = {
        os.getenv("STRIPE_CREATOR_PRICE_ID"): "creator",
        os.getenv("STRIPE_SMALL_TEAM_PRICE_ID"): "small_team",
        os.getenv("STRIPE_AGENCY_PRICE_ID"): "agency",
        os.getenv("STRIPE_ENTERPRISE_PRICE_ID"): "enterprise"
    }
    return price_map.get(price_id)    
            
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
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
