"""Render and send emails.

Templates live in meetmind/templates/email/<name>.html and <name>.txt (Jinja2).
Every email has an HTML part and a plain-text part, so it reads well in any client.
HTML templates are auto-escaped: user content (meeting titles, names) can't inject markup.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .config import Settings

TEMPLATE_DIR = Path(__file__).parent / "templates" / "email"


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
        undefined=StrictUndefined,  # a missing variable is a bug, not an empty string
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(template: str, /, **context) -> tuple[str, str]:
    """Returns (html, text) for `template` (positional-only, so a context variable may be called `name`)."""
    env = _env()
    return env.get_template(f"{template}.html").render(**context), env.get_template(f"{template}.txt").render(**context)


def clean_header(value: str, limit: int = 200) -> str:
    """Header values must be one line (prevents header injection via meeting titles etc.)."""
    return " ".join(value.split())[:limit]


def build_message(settings: Settings, to: str, subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = clean_header(to, 320)
    msg["Subject"] = clean_header(subject)
    msg["Date"] = formatdate(localtime=False)
    domain = parseaddr(settings.mail_from)[1].rpartition("@")[2] or "meetmind.local"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_now(settings: Settings, to: str, subject: str, html: str, text: str) -> None:
    """Deliver one email over SMTP. Called by the background worker (see jobs.send_email)."""
    msg = build_message(settings, to, subject, html, text)
    context = ssl.create_default_context()
    if settings.smtp_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if settings.smtp_starttls and not settings.smtp_ssl:
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)
