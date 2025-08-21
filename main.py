import datetime
from datetime import timedelta
import os
import secrets
import hashlib
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, BackgroundTasks, Header, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy import LargeBinary
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, ProgrammingError 
from sqlalchemy.orm import defer
from sqlalchemy import text
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

class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow())
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User")

# Database models for email verification
class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime)
    verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)  # Add this line
    user = relationship("User")

# Helper functions for password reset
def generate_reset_token():
    """Generate secure password reset token"""
    return secrets.token_urlsafe(32)

def create_password_reset_record(user_id: int, db):
    """Create password reset record"""
    # Invalidate any existing tokens for this user
    existing_tokens = db.query(PasswordReset).filter(
        PasswordReset.user_id == user_id,
        PasswordReset.used == False,
        PasswordReset.expires_at > datetime.datetime.utcnow()  # Fixed this line
    ).all()
    
    for token in existing_tokens:
        token.used = True
        token.used_at = datetime.datetime.utcnow()  # Fixed this line
    
    # Create new token
    token = generate_reset_token()
    reset_record = PasswordReset(
        user_id=user_id,
        token=token,
        expires_at=datetime.datetime.utcnow() + timedelta(hours=1)  # Fixed this line
    )
    db.add(reset_record)
    db.commit()
    return reset_record

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
        expires_at=datetime.datetime.utcnow() + timedelta(hours=24)
    )
    db.add(verification)
    db.commit()
    return verification

class EmailService:
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.smtp_username = os.getenv("SMTP_USERNAME")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("EMAIL_FROM", "noreply@giverai.me")
        self.sender_name = os.getenv("EMAIL_SENDER_NAME", "GiverAI")

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

    def send_password_reset_email(self, user, reset_token, ip_address="Unknown"):
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
            "Reset Your GiverAI Password 🔒",
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
                <p><strong>When:</strong> {datetime.datetime.utcnow().strftime('%B %d, %Y at %I:%M %p UTC')}</p>
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

    def send_verification_email(self, user, verification_token):
        """Send verification email with simple template"""
        verification_code = verification_token[-6:]
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
              <p>Or use this verification code: <strong>{verification_code}</strong></p>
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

    def send_welcome_email(self, user):
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
            html_body
        )

    def send_email(self, to_email: str, template_name: str, **kwargs) -> bool:
        """Send email using specified template"""
        try:
            if not all([self.smtp_server, self.smtp_username, self.smtp_password]):
                print("⛔ Missing email configuration")
                return False
                
            template = EMAIL_TEMPLATES.get(template_name)
            if not template:
                print(f"⛔ Email template '{template_name}' not found")
                return False
            
            # Format template with provided variables
            subject = template["subject"].format(**kwargs)
            body = template["body"].format(**kwargs)
            
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{self.sender_name} <{self.from_email}>"
            msg['To'] = to_email
            msg['Reply-To'] = self.from_email
            
            # Add HTML body
            html_part = MIMEText(body, 'html')
            msg.attach(html_part)
            
            # Send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)
                
            print(f"✅ {template_name.title()} email sent to {to_email}")
            return True
            
        except Exception as e:
            print(f"⛔ Failed to send {template_name} email: {str(e)}")
            return False

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
                        <p style="margin: 5px 0;"><strong>When:</strong> {datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p UTC")}</p>
                        <p style="margin: 5px 0;"><strong>IP Address:</strong> {ip_address}</p>
                    </div>
                    
                    <h3>Was this you?</h3>
                    <p>If you made this change, no action is needed. Your account is secure.</p>
                    
                    <p><strong>If you didn't make this change:</strong></p>
                    <ol>
                        <li>Change your password immediately</li>
                        <li>Review your account settings</li>
                        <li>Contact our support team</li>
                    </ol>
                    
                    <p style="text-align: center;">
                        <a href="https://giverai.me/account" 
                           style="display: inline-block; background: #dc3545; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                            Secure My Account
                        </a>
                    </p>
                    
                    <p>If you need help, please contact us at support@giverai.me</p>
                    
                    <p>Stay secure,<br><strong>The GiverAI Security Team</strong></p>
                </div>
                
                <div style="text-align: center; margin-top: 20px; color: #666; font-size: 12px;">
                    <p>This security alert was sent to {user.email}</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return self.send_simple_email(
            user.email,
            "Your GiverAI Account Was Updated 🔒",
            html_body
        )
    
    def send_subscription_changed_email(self, user, old_plan, new_plan, next_billing_date, amount):
        """Send subscription update notification"""
        plan_features = get_plan_features(new_plan)
        feature_list = []
        
        if plan_features["daily_limit"] == float('inf'):
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
            "enterprise": "Complete solution for large organizations"
        }
        
        return self.send_email(
            to_email=user.email,
            template_name="subscription_changed",
            username=user.username,
            new_plan=new_plan.replace("_", " ").title(),
            plan_description=plan_descriptions.get(new_plan, ""),
            new_features="<br>".join(feature_list),
            next_billing_date=next_billing_date,
            amount=amount,
            dashboard_url="https://giverai.me/dashboard",
            billing_url="https://giverai.me/account"
        )
    
    def send_team_invitation_email(self, invitee_email, team_owner, role, team_name, team_plan, team_industry, accept_token, decline_token):
        """Send team invitation email"""
        role_permissions = {
            "admin": "Full access to team settings and member management",
            "editor": "Can create and edit team content",
            "viewer": "Can view team content and analytics"
        }
        
        return self.send_email(
            to_email=invitee_email,
            template_name="team_invitation",
            team_owner=team_owner,
            role=role.title(),
            role_permissions=role_permissions.get(role, "Team collaboration access"),
            team_name=team_name or f"{team_owner}'s Team",
            team_plan=team_plan.replace("_", " ").title(),
            team_industry=team_industry or "Not specified",
            email=invitee_email,
            accept_url=f"https://giverai.me/accept-invitation?token={accept_token}",
            decline_url=f"https://giverai.me/decline-invitation?token={decline_token}"
        )
    
    def send_goodbye_email(self, user, total_tweets, days_active):
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
                        <li>🎯 Used the {user.plan.replace('_', ' ').title()} plan</li>
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

# Initialize email service
email_service = EmailService()

def test_simple_email(to_email: str):
    """Simple email test with minimal HTML"""
    try:
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_username = os.getenv("SMTP_USERNAME")
        smtp_password = os.getenv("SMTP_PASSWORD")
        from_email = os.getenv("EMAIL_FROM", "noreply@giverai.me")
        
        print(f"📧 Email Config:")
        print(f"   Server: {smtp_server}")
        print(f"   Port: {smtp_port}")
        print(f"   Username: {smtp_username}")
        print(f"   From: {from_email}")
        print(f"   To: {to_email}")
        
        if not all([smtp_server, smtp_username, smtp_password]):
            print("⛔ Missing email configuration")
            return False
        
        # Create simple message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = "Test Email from GiverAI"
        msg['From'] = from_email
        msg['To'] = to_email
        
        # Simple HTML body
        html_body = """
        <html>
        <body>
            <h2>Test Email</h2>
            <p>This is a test email from GiverAI.</p>
            <p>If you see this, the email service is working!</p>
        </body>
        </html>
        """
        
        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)
        
        # Send email
        print("📤 Connecting to SMTP server...")
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            print("🔒 Starting TLS...")
            server.starttls()
            print("🔑 Logging in...")
            server.login(smtp_username, smtp_password)
            print("📧 Sending message...")
            server.send_message(msg)
            print("✅ Email sent successfully!")
            
        return True
        
    except Exception as e:
        print(f"⛔ Email error: {str(e)}")
        print(f"   Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        return False

# ----- Auth Setup -----
SECRET_KEY = "your-secret-key-here"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

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
    # We need to load the password for verification
    user = db.query(User).filter(User.username == username).first()
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
        # Use the existing get_user function instead of get_user_safe
        user = get_user(db, username)
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

    if 'password_resets' not in existing_tables:
        print("Creating password_resets table...")
        try:
            Base.metadata.tables['password_resets'].create(bind=engine)
            print("✅ password_resets table created")
        except Exception as e:
            print(f"❌ Error creating password_resets table: {e}")
    
    # Create the missing tables
    if tables_to_create:
        print(f"Creating missing tables: {tables_to_create}")
        try:
            # Create all tables defined in Base.metadata
            Base.metadata.create_all(bind=engine)
            print("All missing tables created successfully")
        except Exception as e:
            print(f"Error creating tables: {e}")
            # Try creating them individually
            for table_name in tables_to_create:
                try:
                    if table_name in Base.metadata.tables:
                        Base.metadata.tables[table_name].create(bind=engine)
                        print(f"Created {table_name} table")
                except Exception as table_error:
                    print(f"Error creating {table_name}: {table_error}")
    
    print("Database migration completed")

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

# Run migrations
migrate_database()
                                 
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
                        <li>🎯 Used the {last_plan} plan</li>
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
