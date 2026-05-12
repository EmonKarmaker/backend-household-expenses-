"""Email service (Resend SDK v2).

All SDK calls run in asyncio.to_thread() — the resend library is synchronous
and would block the event loop if called directly.

Security rule: temp_password and reset_token are NEVER written to any
logger call in this module.  The dev console stub may print them to stdout,
but only when APP_ENV=development, and never via the logging system.
"""
import asyncio
import logging
import uuid
from html import escape

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


def _reset_bodies(to_name: str, reset_url: str) -> tuple[str, str]:
    n, ru = escape(to_name), escape(reset_url)
    html = f"""\
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;color:#222;">
  <h2>Reset your password</h2>
  <p>Hi {n},</p>
  <p>We received a password-reset request for your
     <strong>{escape(settings.email_from_name)}</strong> account.
     Click the button below to set a new password:</p>
  <p style="margin:24px 0;">
    <a href="{ru}"
       style="background:#2563eb;color:#fff;padding:10px 22px;border-radius:6px;
              text-decoration:none;font-weight:bold;display:inline-block;">
      Reset password
    </a>
  </p>
  <p style="color:#666;font-size:14px;">
    This link expires in <strong>30 minutes</strong>.
    If you did not request a reset, you can safely ignore this email —
    your password will not change.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="color:#999;font-size:12px;">
    If the button does not work, paste this URL into your browser:<br>
    <span style="word-break:break-all;">{ru}</span>
  </p>
</div>"""
    text = (
        f"Hi {to_name},\n\n"
        f"Reset your {settings.email_from_name} password by visiting:\n\n"
        f"{reset_url}\n\n"
        f"This link expires in 30 minutes.\n"
        f"If you did not request a reset, ignore this email."
    )
    return html, text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_invite_email(
    to_email: str,
    to_name: str,
    temp_password: str,
    login_url: str,
) -> str:
    """Send a flatmate invitation with their temporary password.

    Returns the Resend message ID. Raises RuntimeError on send failure.
    temp_password is never written to any log statement.
    """
    html, text = _invite_bodies(to_name, to_email, temp_password, login_url)
    params: dict = {
        "from": _FROM,
        "to": [to_email],
        "subject": f"You have been added to {settings.email_from_name}",
        "html": html,
        "text": text,
    }
    try:
        msg_id = await asyncio.to_thread(_send_fn, params)
    except Exception as exc:
        logger.error("Invite email failed for %s: %s", to_email, exc)
        raise RuntimeError(f"Could not send invite email to {to_email}") from exc
    logger.info("Invite email sent to %s (msg_id=%s)", to_email, msg_id)
    return msg_id


async def send_password_reset_email(
    to_email: str,
    to_name: str,
    reset_token: str,  # noqa: ARG001 — caller passes it; not used here, not logged
    reset_url: str,
) -> str:
    """Send a password-reset link.

    reset_url already contains the token embedded by the caller.
    reset_token is accepted in the signature for consistency with the auth
    router interface but is never logged or written to any output here.

    Returns the Resend message ID. Raises RuntimeError on send failure.
    """
    html, text = _reset_bodies(to_name, reset_url)
    params: dict = {
        "from": _FROM,
        "to": [to_email],
        "subject": f"Reset your {settings.email_from_name} password",
        "html": html,
        "text": text,
    }
    try:
        msg_id = await asyncio.to_thread(_send_fn, params)
    except Exception as exc:
        logger.error("Password reset email failed for %s: %s", to_email, exc)
        raise RuntimeError(f"Could not send password reset email to {to_email}") from exc
    logger.info("Password reset email sent to %s (msg_id=%s)", to_email, msg_id)
    return msg_id
