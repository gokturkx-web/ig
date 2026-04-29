"""Cevap çözümleyicisi için saf birim testler (ağ yok)."""

from ig_scanner.checker import _interpret, UsernameStatus


def _payload(username_errors=None, extra=None):
    errs = {"email": [{"code": "invalid_email", "message": "x"}]}
    if username_errors is not None:
        errs["username"] = username_errors
    payload = {"errors": errs, "status": "ok", "error_type": "form_validation_error"}
    if extra:
        payload.update(extra)
    return payload


def test_available_when_no_username_error():
    r = _interpret("foobar", _payload())
    assert r.status == UsernameStatus.AVAILABLE
    assert r.code is None


def test_taken():
    r = _interpret(
        "cristiano",
        _payload([{"code": "username_is_taken", "message": "taken"}]),
    )
    assert r.status == UsernameStatus.TAKEN
    assert r.code == "username_is_taken"


def test_reserved():
    r = _interpret(
        "instagram",
        _payload([{"code": "username_invalid", "message": "exists"}]),
    )
    assert r.status == UsernameStatus.RESERVED


def test_blocked_substring():
    r = _interpret(
        "admin",
        _payload([{"code": "username_invalid_substring", "message": "exists"}]),
    )
    assert r.status == UsernameStatus.BLOCKED_TERM


def test_unknown_error_code():
    r = _interpret(
        "weird",
        _payload([{"code": "future_unknown_code", "message": "?"}]),
    )
    assert r.status == UsernameStatus.UNKNOWN
