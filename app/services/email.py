"""Email service — Resend SDK (invite emails) + Gmail SMTP (password reset).

All blocking I/O runs in asyncio.to_thread() so the event loop is never stalled.

Security rules:
- temp_password and reset_token are NEVER written to any logger call.
- GMAIL_APP_PASSWORD is never included in error messages or repr output.
- The dev console stub may print sensitive values to stdout only when
  APP_ENV=development, and never via the logging system.
"""
import asyncio
import logging
import smtplib
import ssl
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import httpx
import resend

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

resend.api_key = settings.resend_api_key
_FROM = f"{settings.email_from_name} <{settings.email_from}>"

# Real Resend API keys always start with "re_".
# Any other value in dev mode activates the console stub so the app works
# without a live Resend account on a dev machine.
_USE_CONSOLE = (
    settings.app_env == "development"
    and not settings.resend_api_key.startswith("re_")
)


# ---------------------------------------------------------------------------
# Internal send backends
# ---------------------------------------------------------------------------

def _resend_send(params: dict) -> str:
    result = resend.Emails.send(params)
    return result.id


def _console_send(params: dict) -> str:
    """Dev-only stub: prints email to stdout. Never runs in production."""
    fake_id = f"dev-{uuid.uuid4().hex[:8]}"
    sep = "-" * 62
    print(f"\n{sep}")
    print(f"[DEV EMAIL] To:      {params['to']}")
    print(f"[DEV EMAIL] Subject: {params['subject']}")
    print(f"[DEV EMAIL] Body:\n{params.get('text', '(no text body)')}")
    print(f"[DEV EMAIL] ID: {fake_id}")
    print(f"{sep}\n")
    return fake_id


_send_fn = _console_send if _USE_CONSOLE else _resend_send


# ---------------------------------------------------------------------------
# HTML + plain-text builders
# ---------------------------------------------------------------------------

def _invite_bodies(
    to_name: str,
    to_email: str,
    temp_password: str,
    login_url: str,
) -> tuple[str, str]:
    n, e, lu = escape(to_name), escape(to_email), escape(login_url)
    # temp_password goes into the email body — that is its purpose.
    # It is NOT passed to any logger call anywhere in this file.
    html = f"""\
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;color:#222;">
  <h2 style="margin-bottom:4px;">Welcome to your household</h2>
  <p>Hi {n},</p>
  <p>You have been added to your household on
     <strong>{escape(settings.email_from_name)}</strong>.
     Use the credentials below to log in for the first time.</p>
  <table style="border-collapse:collapse;margin:16px 0;">
    <tr>
      <td style="padding:6px 16px 6px 0;color:#666;">Email</td>
      <td><strong>{e}</strong></td>
    </tr>
    <tr>
      <td style="padding:6px 16px 6px 0;color:#666;">Temporary password</td>
      <td>
        <code style="background:#f4f4f4;padding:4px 10px;border-radius:4px;
                     font-size:15px;letter-spacing:1px;">{temp_password}</code>
      </td>
    </tr>
  </table>
  <p style="margin:24px 0;">
    <a href="{lu}"
       style="background:#2563eb;color:#fff;padding:10px 22px;border-radius:6px;
              text-decoration:none;font-weight:bold;display:inline-block;">
      Log in now
    </a>
  </p>
  <p style="color:#888;font-size:13px;">
    You will be prompted to set a new password on first login.
  </p>
</div>"""
    text = (
        f"Hi {to_name},\n\n"
        f"You have been added to your household on {settings.email_from_name}.\n\n"
        f"Email:             {to_email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"Log in at: {login_url}\n\n"
        f"You will be prompted to set a new password on first login."
    )
    return html, text


# ---------------------------------------------------------------------------
# Gmail SMTP backend (password reset emails)
# ---------------------------------------------------------------------------

def _send_smtp_blocking(to: str, subject: str, html_body: str, text_body: str) -> None:
    """Synchronous Gmail SMTP sender — call only via asyncio.to_thread()."""
    logger.info("smtp: building message from %s to %s", settings.gmail_sender, to)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_sender
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    ctx = ssl.create_default_context()
    try:
        logger.info("smtp: connecting to smtp.gmail.com:465")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
            logger.info("smtp: connected, logging in as %s", settings.gmail_sender)
            smtp.login(settings.gmail_sender, settings.gmail_app_password)
            logger.info("smtp: login successful, sending message")
            smtp.send_message(msg)
            logger.info("smtp: send_message returned cleanly to %s", to)
    except Exception as exc:
        # Never include credentials in the error message.
        raise RuntimeError(f"Failed to send email to {to}") from exc


async def send_email(to: str, subject: str, html_body: str, text_body: str) -> None:
    """Async wrapper — runs the blocking SMTP call on a thread pool."""
    await asyncio.to_thread(_send_smtp_blocking, to, subject, html_body, text_body)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_invite_email(
    to_email: str,
    to_name: str,
    temp_password: str,
    login_url: str,
) -> str:
    """Send a flatmate invitation with their temporary password via Brevo HTTP API.

    Returns the Brevo message ID. Raises RuntimeError on send failure.
    temp_password is never written to any log statement.
    """
    html, text = _invite_bodies(to_name, to_email, temp_password, login_url)
    payload = {
        "sender": {
            "email": settings.brevo_sender_email,
            "name": settings.brevo_sender_name,
        },
        "to": [{"email": to_email}],
        "subject": f"You have been added to {settings.email_from_name}",
        "htmlContent": html,
        "textContent": text,
    }
    headers = {
        "api-key": settings.brevo_api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }

    logger.info("brevo: sending invite to %s via Brevo HTTP API", to_email)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.error("brevo: HTTP transport error sending invite to %s: %s", to_email, exc)
        raise RuntimeError(f"Could not send invite email to {to_email}") from exc

    if resp.status_code >= 400:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        logger.error("brevo: invite send to %s failed with status %s: %s", to_email, resp.status_code, err_body)
        raise RuntimeError(f"Could not send invite email to {to_email}")

    message_id = resp.json().get("messageId")
    logger.info("brevo: invite to %s succeeded (messageId=%s)", to_email, message_id)
    return str(message_id) if message_id is not None else ""


async def send_password_reset_email(to: str, reset_link: str) -> None:
    """Send a password-reset link via Brevo HTTP API.

    reset_link already contains the raw token embedded by the caller.
    The link is included in the email body (that is its purpose) and is
    never written to any log statement.

    Raises RuntimeError on send failure.
    """
    expiry = settings.reset_token_expiry_hours
    unit = "hour" if expiry == 1 else "hours"
    rl = escape(reset_link)
    html = f"""\
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;color:#222;">
  <h2>Reset your HomieGhor password</h2>
  <p>We received a request to reset the password for your HomieGhor account.</p>
  <p style="margin:24px 0;">
    <a href="{rl}"
       style="background:#2563eb;color:#fff;padding:10px 22px;border-radius:6px;
              text-decoration:none;font-weight:bold;display:inline-block;">
      Reset password
    </a>
  </p>
  <p style="color:#666;font-size:14px;">
    This link expires in <strong>{expiry} {unit}</strong>.
    If you did not request a reset, you can safely ignore this email —
    your password will not change.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="color:#999;font-size:12px;">
    If the button does not work, paste this URL into your browser:<br>
    <span style="word-break:break-all;">{rl}</span>
  </p>
</div>"""
    text = (
        f"Reset your HomieGhor password by visiting:\n\n"
        f"{reset_link}\n\n"
        f"This link expires in {expiry} {unit}.\n"
        f"If you did not request a reset, ignore this email."
    )

    payload = {
        "sender": {
            "email": settings.brevo_sender_email,
            "name": settings.brevo_sender_name,
        },
        "to": [{"email": to}],
        "subject": "Reset your HomieGhor password",
        "htmlContent": html,
        "textContent": text,
    }
    headers = {
        "api-key": settings.brevo_api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }

    logger.info("brevo: sending password reset to %s via Brevo HTTP API", to)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.error("brevo: HTTP transport error sending to %s: %s", to, exc)
        raise RuntimeError(f"Failed to send email to {to}") from exc

    if resp.status_code >= 400:
        # Brevo returns JSON error details; log the status + body but NEVER the api-key.
        # The api-key is in the request headers we just sent, not in the response, so the
        # response body is safe to log.
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        logger.error("brevo: send to %s failed with status %s: %s", to, resp.status_code, err_body)
        raise RuntimeError(f"Failed to send email to {to}")

    try:
        message_id = resp.json().get("messageId")
    except Exception:
        message_id = None
    logger.info("brevo: send to %s succeeded (messageId=%s)", to, message_id)
