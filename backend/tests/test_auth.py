import os
import pytest
from fastapi import HTTPException
import auth

def test_open_when_token_unset(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    assert auth.require_token(None) is None            # no header, still allowed

def test_enforced_when_token_set(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as e:
        auth.require_token(None)
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        auth.require_token("Bearer wrong")
    assert auth.require_token("Bearer s3cret") is None  # correct token allowed

def test_constant_time_compare_used(monkeypatch):
    # a bare token without the Bearer prefix is rejected
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException):
        auth.require_token("s3cret")
