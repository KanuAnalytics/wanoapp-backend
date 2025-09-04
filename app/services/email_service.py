"""
Email service using SendGrid

app/services/email_service.py
"""
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content
from app.core.config import settings
import logging
import secrets

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        try:
            self.sg = sendgrid.SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
            self.from_email = Email(settings.SENDGRID_FROM_EMAIL, "Wano Team")
            self.is_configured = True
        except Exception as e:
            logger.warning(f"SendGrid not configured: {e}")
            self.is_configured = False

    def _generate_otp(self, length: int = 6) -> str:
        """Generate a cryptographically secure numeric OTP of the given length."""
        digits = "0123456789"
        return "".join(secrets.choice(digits) for _ in range(length))
    
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

    async def send_password_reset_otp(self, to_email: str, username: str, otp: str | None = None, expiry_minutes: int = 30):
        """
        Send a 6-digit OTP to the user for password reset.
        If `otp` is not provided, a secure 6-digit code will be generated and returned.
        Returns a tuple: (success: bool, otp: str)
        """
        # Prepare OTP
        otp_code = otp or self._generate_otp(6)

        if not self.is_configured:
            logger.warning("SendGrid not configured. Using OTP above for testing.")
            # Return True to avoid blocking flows in non-email environments
            return True, otp_code

        subject = "Your WanoApp password reset code"
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #333;">Password reset request</h2>
                <p>Hi {username},</p>
                <p>Use the following one-time code to reset your password:</p>
                <div style="text-align: center; margin: 24px 0;">
                    <div style="display: inline-block; font-size: 28px; letter-spacing: 6px; font-weight: bold; padding: 12px 20px; border: 1px dashed #ccc; border-radius: 8px;">
                        {otp_code}
                    </div>
                </div>
                <p style="color: #666;">For your security, do not share this code with anyone.</p>
                <p style="color: #999; font-size: 14px;">This code will expire in {expiry_minutes} minutes.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">If you didn't request a password reset, you can safely ignore this email.</p>
                <p>Best regards,<br>The WanoApp Team</p>
            </body>
        </html>
        """

        to_email_obj = To(to_email)
        content = Content("text/html", html_content)
        mail = Mail(self.from_email, to_email_obj, subject, content)

        try:
            response = self.sg.send(mail)
            logger.info(f"Password reset OTP sent to {to_email}. Status code: {response.status_code}")
            return True, otp_code
        except Exception as e:
            logger.error(f"Failed to send password reset OTP: {str(e)}")
            return False, otp_code

email_service = EmailService()