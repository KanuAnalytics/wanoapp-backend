"""
Email service using SendGrid

app/services/email_service.py
"""
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        try:
            self.sg = sendgrid.SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
            self.from_email = Email(settings.SENDGRID_FROM_EMAIL)
            self.is_configured = True
        except Exception as e:
            logger.warning(f"SendGrid not configured: {e}")
            self.is_configured = False
    
    async def send_verification_email(self, to_email: str, username: str, verification_token: str):
        """Send verification email to user"""
        # Frontend link (for production)
        frontend_verification_link = f"{settings.FRONTEND_URL}/verify-email?token={verification_token}"
        
        # Direct API link (for testing)
        api_verification_link = f"http://localhost:8000/api/v1/auth/verify-email?token={verification_token}"
        
        # For development, log both links
        logger.info(f"=== Verification Links for {username} ===")
        logger.info(f"Frontend link: {frontend_verification_link}")
        logger.info(f"API link (for testing): {api_verification_link}")
        logger.info(f"Token: {verification_token}")
        logger.info("=====================================")
        
        if not self.is_configured:
            logger.warning("SendGrid not configured. Use links above for testing.")
            return True
        
        subject = "Verify your WanoApp account"
        html_content = f"""
        <html>
            <body>
                <h2>Welcome to WanoApp, {username}!</h2>
                <p>Please click the link below to verify your email address:</p>
                <p><a href="{frontend_verification_link}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Verify Email</a></p>
                <p>Or copy and paste this link in your browser:</p>
                <p>{frontend_verification_link}</p>
                <p>This link will expire in 24 hours.</p>
                <p>Best regards,<br>The WanoApp Team</p>
            </body>
        </html>
        """
        
        to_email_obj = To(to_email)
        content = Content("text/html", html_content)
        mail = Mail(self.from_email, to_email_obj, subject, content)
        
        try:
            # SendGrid's send method is synchronous, not async - removed await
            response = self.sg.send(mail)
            logger.info(f"Verification email sent to {to_email}. Status code: {response.status_code}")
            return True
        except Exception as e:
            logger.error(f"Failed to send verification email: {str(e)}")
            # Return False but don't block registration
            return False

email_service = EmailService()