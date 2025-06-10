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
        # Direct backend API link for verification
        backend_url = getattr(settings, 'BACKEND_URL', 'https://devbe.wanoafrica.com')
        verification_link = f"{backend_url}/api/v1/auth/verify-email?token={verification_token}"
        
        # Log for debugging
        logger.info(f"=== Verification Link for {username} ===")
        logger.info(f"Verification link: {verification_link}")
        logger.info(f"Token: {verification_token}")
        logger.info("=====================================")
        
        if not self.is_configured:
            logger.warning("SendGrid not configured. Use link above for testing.")
            return True
        
        subject = "Verify your WanoApp account"
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #333;">Welcome to WanoApp, {username}!</h2>
                <p>Thank you for signing up. Please verify your email address to start uploading videos.</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{verification_link}" 
                       style="background-color: #4CAF50; 
                              color: white; 
                              padding: 12px 30px; 
                              text-decoration: none; 
                              border-radius: 5px;
                              display: inline-block;
                              font-weight: bold;">
                        Verify Email
                    </a>
                </div>
                <p>Or copy and paste this link in your browser:</p>
                <p style="word-break: break-all; color: #666;">
                    {verification_link}
                </p>
                <p style="color: #999; font-size: 14px;">
                    This link will expire in 24 hours.
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">
                    If you didn't create an account with WanoApp, please ignore this email.
                </p>
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