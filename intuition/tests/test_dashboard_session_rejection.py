"""_is_session_rejection must recognise every "paste a fresh cookie" failure.

When it misses one, State.reject_session is never called, s.session_rejected
stays false, and the dashboard's Authorisation panel never opens - leaving the
user with a sync error and nowhere to enter a new BbRouter cookie.
"""
from intuition import dashboard
from intuition.auth import AuthenticationError


def test_rest_rejection_wording_is_detected():
    exc = AuthenticationError(
        "Your NTU Learn session token was rejected (401 Unauthorized).")
    assert dashboard._is_session_rejection(exc)


def test_expired_session_wording_is_detected():
    exc = AuthenticationError("NTU Learn session token expired.")
    assert dashboard._is_session_rejection(exc)


def test_legacy_login_page_redirect_is_detected():
    exc = AuthenticationError(
        "NTU Learn redirected the legacy course request to a login page. "
        "Refresh the BbRouter cookie.")
    assert dashboard._is_session_rejection(exc)


def test_scope_mismatch_is_not_a_session_rejection():
    exc = AuthenticationError(
        "No courses are labelled 25S1 (the semester in progress). "
        "Checked 6 enrolment(s).")
    assert not dashboard._is_session_rejection(exc)


def test_network_blip_is_not_a_session_rejection():
    exc = AuthenticationError("Could not read your Favourites (Connection timed out).")
    assert not dashboard._is_session_rejection(exc)
