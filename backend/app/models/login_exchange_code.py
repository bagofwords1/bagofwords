"""One-time code that exchanges an SSO redirect for a session token.

The SSO callback used to hand the session JWT back to the browser in the
redirect URL (``/users/sign-in?access_token=<jwt>``). That put a credential
with a 7-day lifetime into browser history, ``Referer`` headers and every
reverse-proxy access log in front of the app — the most likely way a bearer
token gets lifted off a self-hosted install.

Now the callback stores the minted token here, keyed by a short-lived
single-use code, and redirects with only that code. The SPA POSTs it to
``/api/auth/exchange`` once and gets the JWT in the response body. A code that
leaks into a log is worthless: it expires in seconds and dies on first use.
"""

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class LoginExchangeCode(BaseSchema):
    __tablename__ = "login_exchange_codes"

    # SHA-256 of the code — the plaintext only ever exists in the redirect URL
    # and the exchange request, never at rest.
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # The session JWT itself. Short-lived at rest: the row is consumed on first
    # exchange and swept once expired.
    access_token = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)

    user = relationship("User")
