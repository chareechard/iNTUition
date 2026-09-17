"""BbRouter session-token handling.

NTU's identity provider moved from ADFS (scriptable username/password form POST) to
Microsoft Entra ID with MFA, so credentials can no longer be exchanged for a session
non-interactively. Instead the user logs in with a browser once and supplies the
resulting ``BbRouter`` cookie; this module validates it, caches it, and reuses it until
it expires.

The BbRouter cookie is a comma separated list of ``key:value`` pairs, e.g.::

    expires:1786304238,id:E835...,signature:df7b...,site:5ecaf6aa-...,
    timeout:10800,user:6itk...,v:2,xsrf:2cc991e0-...

An unauthenticated (anonymous) BbRouter has no ``user`` field.
"""
import json
import os
import re
import tempfile
import time
from typing import Dict, Optional

from intuition.constants import LOGIN_LANDING_URL

DEFAULT_TOKEN_PATH = os.path.join(
    os.path.expanduser("~"), ".intuition", "session.json"
)

# Refuse to use a token that is about to expire mid-download.
EXPIRY_SAFETY_MARGIN_SECONDS = 120


class AuthenticationError(Exception):
    pass


HOW_TO_GET_TOKEN = """\
iNTUition now authenticates through Microsoft Entra ID with MFA, which cannot be
scripted. To get a session token:

  1. Open {url} in your browser and log in as usual.
  2. Open DevTools (F12) -> Application (Chrome) or Storage (Firefox) -> Cookies
     -> https://ntulearn.ntu.edu.sg
  3. Copy the full value of the "BbRouter" cookie.
  4. Pass it with --bbrouter "<value>" (it will be cached for reuse).

The token is valid for a few hours; rerun these steps when it expires.\
""".format(
    url=LOGIN_LANDING_URL
)


def parse_bbrouter(BbRouter: str) -> Dict[str, str]:
    """Parse a BbRouter cookie value into a dict.

    Values may themselves contain no commas, but ``id``/``signature`` can contain
    colons is not observed in practice; split on the first colon only to be safe.
    """
    fields: Dict[str, str] = {}
    for part in BbRouter.split(","):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _cookie_value(value: str) -> str:
    """Extract a BbRouter value from common browser copy formats."""
    value = value.strip().strip('"').strip("'").strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    match = re.search(r"(?:^|;)\s*bbrouter\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"').strip("'").strip()
    if value.lower().startswith("bbrouter="):
        value = value[len("bbrouter="):].strip()
    return value.rstrip(";").strip().strip('"').strip("'").strip()


def is_authenticated(BbRouter: str) -> bool:
    """A BbRouter is only useful if it carries a ``user`` field."""
    if not isinstance(BbRouter, str) or not BbRouter:
        return False
    return "user" in parse_bbrouter(BbRouter)


def expires_at(BbRouter: str) -> Optional[int]:
    """Unix timestamp at which the token expires, or None if not stated."""
    raw = parse_bbrouter(BbRouter).get("expires")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_expired(BbRouter: str, now: Optional[float] = None) -> bool:
    exp = expires_at(BbRouter)
    if exp is None:
        # No expiry field: cannot prove it is stale, let the server decide.
        return False
    now = time.time() if now is None else now
    return exp <= now + EXPIRY_SAFETY_MARGIN_SECONDS


def validate(BbRouter: str) -> str:
    """Return the token if it is usable, otherwise raise AuthenticationError."""
    if not isinstance(BbRouter, str) or not BbRouter.strip():
        raise AuthenticationError("No BbRouter token supplied.\n\n" + HOW_TO_GET_TOKEN)
    BbRouter = _cookie_value(BbRouter)
    if not is_authenticated(BbRouter):
        raise AuthenticationError(
            "BbRouter has no 'user' field, it is an anonymous session token.\n\n"
            + HOW_TO_GET_TOKEN
        )
    if is_expired(BbRouter):
        raise AuthenticationError(
            "BbRouter expired at {}.\n\n{}".format(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at(BbRouter))),
                HOW_TO_GET_TOKEN,
            )
        )
    return BbRouter


def save_token(BbRouter: str, path: str = DEFAULT_TOKEN_PATH) -> str:
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"BbRouter": BbRouter}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best effort; not all filesystems support this (e.g. some Windows setups).
        pass
    return path


def load_token(path: str = DEFAULT_TOKEN_PATH) -> Optional[str]:
    """Return a cached token if one exists and is still usable, else None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            BbRouter = json.load(f).get("BbRouter")
    except (AttributeError, TypeError, ValueError, OSError):
        return None
    try:
        return validate(BbRouter)
    except AuthenticationError:
        return None


def resolve(
    BbRouter: Optional[str] = None,
    token_path: str = DEFAULT_TOKEN_PATH,
    use_cache: bool = True,
) -> str:
    """Resolve a usable token from (in order) the argument then the cache.

    A token supplied explicitly is validated and then cached for next time.
    """
    if BbRouter:
        token = validate(BbRouter)
        if use_cache:
            # Caching is a convenience, not part of authentication.  A packaged or
            # sandboxed dashboard may be allowed to serve its download directory but
            # not write beneath the user's home directory.  Keep the valid token in
            # the live session instead of aborting the HTTP request in that case.
            try:
                save_token(token, token_path)
            except OSError:
                pass
        return token

    if use_cache:
        cached = load_token(token_path)
        if cached:
            return cached

    raise AuthenticationError(
        "No valid cached session found.\n\n" + HOW_TO_GET_TOKEN
    )

