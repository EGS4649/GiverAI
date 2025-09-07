import datetime
from datetime import datetime, timedelta, date
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
    generated_at = Column(DateTime, default=datetime.utcnow)
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
    created_at = Column(DateTime, default=datetime.utcnow())
    expires_at = Column(DateTime)
    used = Column(Boolean, default=False)
    used_at = Column(DateTime, nullable=True)
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
        PasswordReset.expires_at > datetime.utcnow()  # Fixed this line
    ).all()
    
    for token in existing_tokens:
        token.used = True
        token.used_at = datetime.utcnow()  # Fixed this line
    
    # Create new token
    token = generate_reset_token()
    reset_record = PasswordReset(
        user_id=user_id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(hours=1)  # Fixed this line
    )
    db.add(reset_record)
    db.commit()
    return reset_record

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    email = Column(String, index=True)
    role = Column(String, default="editor")
    status = Column(String, default="pending")  # pending, active, removed
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")

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
            "Reset Your GiverAI Password 🔑",
            html_body
        )
        
    def send_verification_email(self, user, verification_token):
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
            html_body,
        )

    def send_subscription_upgrade_email(self, user, old_plan, new_plan, amount, next_billing_date):
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

    def test_simple_email(self, to_email: str):
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


# Initialize email service
email_service = EmailService()

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
            'posting_frequency': "ALTER TABLE users ADD COLUMN posting_frequency VARCHAR",
            'original_plan': "ALTER TABLE users ADD COLUMN original_plan VARCHAR"
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

# ---- ROUTES ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/register", response_class=HTMLResponse)
def register(request: Request, success: str = None):
    user = get_optional_user(request)
    success_message = None
    
    if success == "registration_complete":
        success_message = "Registration successful! Please check your email to verify your account."
    
    return templates.TemplateResponse("register.html", {
        "request": request, 
        "user": user,
        "success": success_message
    })

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
                "user": None,
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
                "user": None,
                "error": "Email already registered"
            })
        
        # Hash the password
        print("Hashing password...")
        hashed_password = hash_password(password)
        print(f"Password hashed successfully, type: {type(hashed_password)}")
        
        # Insert user
        result = db.execute(text("""
            INSERT INTO users (username, email, hashed_password, plan, is_active, created_at)
            VALUES (:username, :email, :hashed_password, :plan, :is_active, :created_at)
            RETURNING id
        """), {
            "username": username,
            "email": email, 
            "hashed_password": hashed_password,
            "plan": "free",
            "is_active": False,
            "created_at": datetime.utcnow()
        })
        
        user_id = result.fetchone()[0]
        db.commit()
        
        # Create email verification record
        verification = create_verification_record(user_id, db)
        
        # Send verification email
        db_user = db.query(User).filter(User.id == user_id).first()
        if db_user:
            try:
                email_service.send_verification_email(db_user, verification.token)
                print("✅ Verification email sent successfully")
            except Exception as e:
                print(f"❌ Failed to send verification email: {str(e)}")
        
        # Also send welcome email
        try:
            email_service.send_welcome_email(db_user)
            print("✅ Welcome email sent successfully")
        except Exception as e:
            print(f"❌ Failed to send welcome email: {str(e)}")
        
        print(f"User registered successfully with ID: {user_id}")
        
        # Return success template instead of redirect
        return templates.TemplateResponse("register_success.html", {
            "request": request,
            "user": None,
            "username": username,
            "email": email
        })
        
    except Exception as e:
        db.rollback()
        error_msg = str(e)
        print(f"Registration error: {error_msg}")
        import traceback
        traceback.print_exc()
        
        return templates.TemplateResponse("register.html", {
            "request": request,
            "user": None,
            "error": "Registration failed. Please try again."
        })
    finally:
        db.close()

# Forgot Password/Username Form
@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_get(request: Request):
    """Display the forgot password form"""
    user = get_optional_user(request)
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "user": user
    })
                      
@app.post("/forgot-password")
async def forgot_password_post(
    request: Request,
    email_or_username: str = Form(...),
    reset_type: str = Form(...)  # 'password' or 'username'
):
    db = SessionLocal()
    try:
        # Get IP address
        ip_address = request.client.host if request.client else "Unknown"
        
        # Find user by email or username
        user = db.query(User).filter(
            (User.email == email_or_username) | (User.username == email_or_username)
        ).first()
        
        if not user:
            # Don't reveal if user exists or not for security
            return templates.TemplateResponse("forgot_password.html", {
                "request": request,
                "user": None,
                "success": "If an account with that email/username exists, we've sent you an email."
            })
        
        if reset_type == "password":
            # Send password reset email
            reset_record = create_password_reset_record(user.id, db)
            try:
                email_service.send_password_reset_email(user, reset_record.token, ip_address)
                success_message = "Password reset email sent! Check your inbox."
            except Exception as e:
                print(f"Failed to send password reset email: {str(e)}")
                success_message = "If an account exists, we've sent a reset email."
                
        elif reset_type == "username":
            # Send username reminder email
            try:
                email_service.send_username_reminder_email(user)
                success_message = "Username reminder sent! Check your inbox."
            except Exception as e:
                print(f"Failed to send username reminder: {str(e)}")
                success_message = "If an account exists, we've sent a username reminder."
        
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "user": None,
            "success": success_message
        })
        
    finally:
        db.close()

# Reset Password Form
@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_get(request: Request, token: str = Query(...)):
    db = SessionLocal()
    try:
        # Verify token
        reset_record = db.query(PasswordReset).filter(
            PasswordReset.token == token,
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
            "token": token,
            "username": reset_record.user.username
        })
    finally:
        db.close()

@app.post("/reset-password")
async def reset_password_post(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...)
):
    db = SessionLocal()
    try:
        # Get IP address
        ip_address = request.client.host if request.client else "Unknown"
        
        if new_password != confirm_password:
            return templates.TemplateResponse("reset_password.html", {
                "request": request,
                "token": token,
                "error": "Passwords don't match"
            })
        
        if len(new_password) < 8:
            return templates.TemplateResponse("reset_password.html", {
                "request": request,
                "token": token,
                "error": "Password must be at least 8 characters long"
            })
        
        # Verify and use token
        reset_record = db.query(PasswordReset).filter(
            PasswordReset.token == token,
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
        ip_address = request.client.host if request.client else "Unknown"
        
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
        user = authenticate_user(db, username, password)
        if not user:
            return templates.TemplateResponse("login.html", {
                "request": request, 
                "error": "Invalid credentials"
            })
        
        # Check if email is verified
        if not user.is_active:
            return templates.TemplateResponse("login.html", {
                "request": request, 
                "error": "Please verify your email before logging in"
            })
        
        access_token = create_access_token(
            data={"sub": user.username},
            expires_delta=timedelta(days=2)
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
        # Use the existing get_user function
        user = get_user(db, username)
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

@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):
    user = get_optional_user(request)
    return templates.TemplateResponse("contact.html", {"request": request, "user": user})

@app.post("/contact", response_class=HTMLResponse)
async def handle_contact_form(request: Request):
    user = get_optional_user(request)
    
    try:
        form = await request.form()
        name = form["name"].strip()
        email = form["email"].strip()
        subject = form["subject"]
        message = form["message"].strip()
        user_info = form.get("user_info", "").strip() if user else None
        
        # Validation
        if not all([name, email, subject, message]):
            raise ValueError("All required fields must be filled out")
            
        if len(message) < 10:
            raise ValueError("Please provide a more detailed message")
            
        # Send emails
        support_sent = email_service.send_contact_form_notification(
            name, email, subject, message, user_info
        )
        
        confirmation_sent = email_service.send_contact_confirmation_email(
            name, email, subject
        )
        
        if support_sent and confirmation_sent:
            return templates.TemplateResponse("contact.html", {
                "request": request,
                "user": user,
                "success": "Thank you! Your message has been sent successfully. We'll get back to you within 24 hours."
            })
        else:
            raise Exception("Failed to send email")
            
    except ValueError as e:
        return templates.TemplateResponse("contact.html", {
            "request": request,
            "user": user,
            "error": str(e)
        })
    except Exception as e:
        print(f"Contact form error: {e}")
        return templates.TemplateResponse("contact.html", {
            "request": request,
            "user": user,
            "error": "Sorry, there was an issue sending your message. Please try again or email us directly at support@giverai.me"
        })
        
@app.get("/debug-email")
def debug_email(email: str = "test@example.com"):
    """Debug email configuration"""
    result = test_simple_email(email)
    return {"status": "success" if result else "failed"}

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

# Fix the change_password route (around line 787):
@app.post("/account/change_password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(get_current_user)
):
    db = SessionLocal()
    try:
        # Get IP address from request
        ip_address = request.client.host if request.client else "Unknown"

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
    db = SessionLocal()
    try:
        # Get IP address from request  
        ip_address = request.client.host if request.client else "Unknown"
        
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


@app.get("/api-docs", response_class=HTMLResponse)
def api_docs(request: Request, user: User = Depends(get_optional_user)):
    return templates.TemplateResponse("api_docs.html", {"request": request, "user": user})

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
        
        response = RedirectResponse("/", status_code=302)
        response.delete_cookie("access_token")
        return response
        
    except Exception as e:
        db.rollback()
        print(f"Error deleting account: {str(e)}")
        # Return user to account page with error
        db_user = apply_plan_features(db_user)
        return templates.TemplateResponse("account.html", {
            "request": request,
            "user": db_user,
            "error": "Failed to delete account. Please try again or contact support."
        })
        
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
        
        # Now mark as canceling
        db_user.plan = "canceling"
        db.commit()

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

        # Get cancellation date from the subscription
        cancellation_date = None
        try:
            subscription = subscriptions.data[0]
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

# Updated checkout endpoint with coming soon restriction
@app.post("/checkout/{plan_type}")
async def create_checkout_session(request: Request, plan_type: str):
    try:
        user = get_current_user(request)
    except HTTPException:
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
            # Commented out for coming soon:
            # "small_team": STRIPE_SMALL_TEAM_PRICE_ID,
            # "agency": STRIPE_AGENCY_PRICE_ID,
            # "enterprise": STRIPE_ENTERPRISE_PRICE_ID
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
        if user.stripe_customer_id:
            # User already has a customer ID, verify it exists in Stripe
            try:
                customer = stripe.Customer.retrieve(user.stripe_customer_id)
                customer_id = customer.id
                print(f"✅ Using existing customer {customer_id} for user {user.email}")
            except stripe.error.InvalidRequestError:
                # Customer doesn't exist in Stripe anymore, create a new one
                print(f"⚠️ Customer {user.stripe_customer_id} not found in Stripe, creating new one")
                user.stripe_customer_id = None
        
        if not customer_id:
            # Create new Stripe customer
            try:
                customer = stripe.Customer.create(
                    email=user.email,
                    name=getattr(user, 'name', None),  # Use name if your User model has it
                    metadata={
                        "user_id": str(user.id),
                        "created_from": "checkout"
                    }
                )
                customer_id = customer.id
                
                # Save the customer ID to the user
                db_user = db.query(User).filter(User.id == user.id).first()
                db_user.stripe_customer_id = customer_id
                db.commit()
                print(f"✅ Created new customer {customer_id} for user {user.email}")
                
                # Update the user object as well so it's available for the checkout session
                user.stripe_customer_id = customer_id
                
            except Exception as e:
                print(f"❌ Failed to create Stripe customer: {str(e)}")
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
            customer=customer_id,  # Use customer ID instead of customer_email
            metadata={
                "user_id": user.id,
                "plan": plan_type
            }
        )
        
        return RedirectResponse(checkout_session.url, status_code=303)
        
    except Exception as e:
        print(f"❌ Error creating checkout session: {str(e)}")
        return templates.TemplateResponse("pricing.html", {
            "request": request,
            "error": f"Error creating checkout session: {str(e)}",
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
        
        return {"error": "Failed to generate tweet"}
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
        
        # Get subscription details with proper error handling
        subscription_id = session.subscription
        print(f"🔗 Subscription ID: {subscription_id}")
        
        next_billing_date = None
        amount = 0
        
        if subscription_id:
            try:
                print("📋 Retrieving subscription details...")
                subscription = stripe.Subscription.retrieve(subscription_id)
                
                # Debug: Print subscription object structure
                print(f"🔍 Subscription status: {subscription.status}")
                print(f"🔍 Subscription object keys: {list(subscription.keys()) if hasattr(subscription, 'keys') else 'No keys method'}")
                
                # Check if current_period_end exists and get it safely
                if hasattr(subscription, 'current_period_end'):
                    next_billing_timestamp = subscription.current_period_end
                    next_billing_date = datetime.fromtimestamp(next_billing_timestamp)
                    print(f"✅ Next billing date: {next_billing_date}")
                else:
                    print("⚠️ current_period_end not found in subscription object")
                    # Try accessing it as a dictionary
                    if 'current_period_end' in subscription:
                        next_billing_timestamp = subscription['current_period_end']
                        next_billing_date = datetime.fromtimestamp(next_billing_timestamp)
                        print(f"✅ Next billing date (dict access): {next_billing_date}")
                    else:
                        print("❌ current_period_end not found in subscription")
                        # Fallback: calculate 1 month from now
                        next_billing_date = datetime.now() + timedelta(days=30)
                        print(f"⚠️ Using fallback billing date: {next_billing_date}")
                
                # Get amount safely
                if subscription.items and subscription.items.data:
                    price_obj = subscription.items.data[0].price
                    if hasattr(price_obj, 'unit_amount') and price_obj.unit_amount:
                        amount = price_obj.unit_amount / 100
                        print(f"💰 Amount: ${amount}")
                    else:
                        print("⚠️ Could not get price amount")
                        # Fallback amounts based on plan
                        plan_amounts = {
                            "creator": 9.0,
                            "small_team": 79.0,
                            "agency": 199.0,
                            "enterprise": 499.0
                        }
                        amount = plan_amounts.get(plan_name)
                        print(f"⚠️ Using fallback amount: ${amount}")
                else:
                    print("❌ No subscription items found")
                    
            except Exception as sub_error:
                print(f"❌ Error retrieving subscription: {str(sub_error)}")
                import traceback
                traceback.print_exc()
                
                # Set fallback values
                next_billing_date = datetime.now() + timedelta(days=30)
                plan_amounts = {
                    "creator": 9.0,
                    "small_team": 25.0,
                    "agency": 69.0,
                    "enterprise": 199.0
                }
                amount = plan_amounts.get(plan_name)
                print(f"⚠️ Using fallback values - Date: {next_billing_date}, Amount: ${amount}")
        else:
            print("❌ No subscription found in session")
        
        # Update user's plan in database
        db = SessionLocal()
        try:
            user = get_optional_user(request)
            print(f"👤 Current user: {user.username if user else 'None'}")
            
            if user:
                db_user = db.query(User).filter(User.id == user.id).first()
                if db_user:
                    old_plan = db_user.plan
                    print(f"📋 Updating plan from {old_plan} to {plan_name}")
                    
                    # Update plan and customer ID
                    db_user.plan = plan_name
                    
                    # Get customer ID from session
                    if session.customer:
                        db_user.stripe_customer_id = session.customer
                        print(f"🆔 Updated customer ID: {session.customer}")
                    
                    db.commit()
                    print("✅ Database updated successfully")
                    
                    # Send upgrade email (even with fallback values)
                    if next_billing_date and amount > 0:
                        try:
                            print("📧 Attempting to send upgrade email...")
                            email_service.send_subscription_upgrade_email(
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

    return templates.TemplateResponse("checkout_success.html", {
        "request": request,
        "user": get_optional_user(request),
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
def dashboard(request: Request, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Check if user needs to complete onboarding
        needs_onboarding = not all([user.role, user.industry, user.goals, user.posting_frequency])
        
        if needs_onboarding:
            # Redirect to onboarding completion
            return RedirectResponse("/complete-onboarding", status_code=302)
        
        # Your existing dashboard logic
        today = str(date.today())
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
        today = str(date.today())
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
                generated_at=datetime.utcnow()
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
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    db = SessionLocal()
    try:
        if event['type'] == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            customer_id = invoice['customer']
            subscription_id = invoice['subscription']
            
            # Get the subscription details to find the next billing date
            subscription = stripe.Subscription.retrieve(subscription_id)
            
            # Find user by customer ID
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                old_plan = user.plan
                
                # Determine new plan from subscription
                new_plan = get_plan_from_price_id(subscription.items.data[0].price.id)
                
                # Update user's plan
                user.plan = new_plan
                db.commit()
                
                # Get the actual next billing date from Stripe
                next_billing_timestamp = subscription.current_period_end
                next_billing_date = datetime.fromtimestamp(next_billing_timestamp)
                
                # Get the amount from the subscription
                amount = subscription.items.data[0].price.unit_amount / 100  # Convert from cents
                
                # Send upgrade email with actual billing date
                try:
                    email_service.send_subscription_upgrade_email(
                        user=user,
                        old_plan=old_plan,
                        new_plan=new_plan,
                        amount=amount,
                        next_billing_date=next_billing_date  # This is now the actual date
                    )
                    print(f"✅ Upgrade email sent to {user.email}")
                except Exception as e:
                    print(f"❌ Failed to send upgrade email: {str(e)}")
        
        return {"status": "success"}
        
    finally:
        db.close()
        
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
        new_plan = original_plan
        
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
            old_plan = user.plan if user.plan != "canceling" else subscription.get('metadata', {}).get('original_plan', 'creator')
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
