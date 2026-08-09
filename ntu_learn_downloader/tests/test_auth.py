import os
import time
import unittest
from tempfile import TemporaryDirectory

from ntu_learn_downloader import auth

AUTHENTICATED = (
    "expires:{expires},id:1A633268311FA435A6HT7K968346A658,"
    "signature:bqguvcoi0nh434robmpzervdtpomolh17rk3m9kxhiy0ozd5tzquhd0e4igldygm,"
    "site:5ecaf6aa-60ca-4431-89e7-6ed4c720440d,timeout:10800,"
    "user:6itk73437hq6tbcznl60t354qc2vn2py,v:2,xsrf:y3d3nzrg-c301-4455-a5a3-hpjdect1jyil"
)

# Shape of the cookie handed out to anonymous visitors by the current deployment.
ANONYMOUS = (
    "expires:{expires},id:E83575893048776F8929DE03DA486EAC,"
    "signature:df7bd869145561a11ecb9e1124acfa402ab03416511116971f9742fd1e11aed7,"
    "site:5ecaf6aa-60ca-4431-89e7-6ed4c720440d,v:2,"
    "xsrf:2cc991e0-62bd-4e61-bcaa-ea812147ae30"
)


def valid_token():
    return AUTHENTICATED.format(expires=int(time.time()) + 3600)


class TestAuth(unittest.TestCase):
    def test_parse_bbrouter(self):
        fields = auth.parse_bbrouter(valid_token())
        self.assertEqual(fields["v"], "2")
        self.assertEqual(fields["user"], "6itk73437hq6tbcznl60t354qc2vn2py")
        self.assertEqual(fields["xsrf"], "y3d3nzrg-c301-4455-a5a3-hpjdect1jyil")

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

    def test_resolve_without_token_raises(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.json")
            with self.assertRaises(auth.AuthenticationError):
                auth.resolve(None, token_path=path)


if __name__ == "__main__":
    unittest.main()
