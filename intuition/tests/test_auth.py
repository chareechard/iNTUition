import os
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import auth

AUTHENTICATED = (
    "expires:{expires},id:AUTHENTICATED_ID,"
    "signature:TEST_SIGNATURE,"
    "site:TEST_SITE_ID,timeout:10800,"
    "user:TEST_USER_ID,v:2,xsrf:TEST_XSRF_TOKEN"
)

# Shape of the cookie handed out to anonymous visitors by the current deployment.
ANONYMOUS = (
    "expires:{expires},id:ANONYMOUS_ID,"
    "signature:ANON_SIGNATURE,"
    "site:TEST_SITE_ID,v:2,"
    "xsrf:ANONYMOUS_XSRF_TOKEN"
)


def valid_token():
    return AUTHENTICATED.format(expires=int(time.time()) + 3600)


class TestAuth(unittest.TestCase):
    def test_parse_bbrouter(self):
        fields = auth.parse_bbrouter(valid_token())
        self.assertEqual(fields["v"], "2")
        self.assertEqual(fields["user"], "TEST_USER_ID")
        self.assertEqual(fields["xsrf"], "TEST_XSRF_TOKEN")

    def test_is_authenticated(self):
        self.assertTrue(auth.is_authenticated(valid_token()))
        self.assertFalse(
            auth.is_authenticated(ANONYMOUS.format(expires=int(time.time()) + 3600))
        )
        self.assertFalse(auth.is_authenticated(""))

    def test_is_expired(self):
        self.assertTrue(
            auth.is_expired(AUTHENTICATED.format(expires=int(time.time()) - 10))
        )
        self.assertFalse(auth.is_expired(valid_token()))
        # Within the safety margin counts as expired.
        self.assertTrue(
            auth.is_expired(AUTHENTICATED.format(expires=int(time.time()) + 30))
        )

    def test_validate_rejects_anonymous(self):
        with self.assertRaises(auth.AuthenticationError):
            auth.validate(ANONYMOUS.format(expires=int(time.time()) + 3600))

    def test_validate_strips_quotes(self):
        self.assertEqual(auth.validate('"{}"'.format(valid_token())), valid_token())

    def test_validate_strips_cookie_name_and_separator(self):
        # What you get from the Network tab's Cookie header rather than from the
        # Application tab's value column.
        self.assertEqual(
            auth.validate("BbRouter={};".format(valid_token())), valid_token()
        )
        self.assertEqual(auth.validate("bbrouter=" + valid_token()), valid_token())

    def test_validate_keeps_expiry_readable_when_named(self):
        # The prefix used to parse into a leading "BbRouter=expires" field, which
        # left the expiry unreadable and the token quietly unusable.
        self.assertIsNotNone(
            auth.expires_at(auth.validate("BbRouter=" + valid_token()))
        )

    def test_save_and_load_roundtrip(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.json")
            token = valid_token()
            auth.save_token(token, path)
            self.assertEqual(auth.load_token(path), token)

    def test_load_ignores_expired(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.json")
            auth.save_token(AUTHENTICATED.format(expires=int(time.time()) - 10), path)
            self.assertIsNone(auth.load_token(path))

    def test_resolve_prefers_argument_and_caches(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.json")
            token = valid_token()
            self.assertEqual(auth.resolve(token, token_path=path), token)
            # Now resolvable with no argument.
            self.assertEqual(auth.resolve(None, token_path=path), token)

    def test_resolve_accepts_valid_token_when_cache_is_unwritable(self):
        token = valid_token()
        with patch.object(auth, "save_token", side_effect=PermissionError("denied")):
            self.assertEqual(auth.resolve(token), token)

    def test_resolve_without_token_raises(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.json")
            with self.assertRaises(auth.AuthenticationError):
                auth.resolve(None, token_path=path)


if __name__ == "__main__":
    unittest.main()
