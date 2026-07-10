import auth

def test_password_hash_and_verify():
    h = auth.hash_password("s3cret-password")
    assert h != "s3cret-password"                 # hashed, not plaintext
    assert auth.verify_password(h, "s3cret-password") is True
    assert auth.verify_password(h, "wrong") is False

def test_jwt_roundtrip_and_reject_garbage():
    t = auth.make_token("a@b.com", "operator")
    p = auth._decode(t)
    assert p["sub"] == "a@b.com" and p["role"] == "operator"
    assert auth._decode("not-a-jwt") is None

def test_role_ranks_ordered():
    assert auth.ROLES["admin"] > auth.ROLES["operator"] > auth.ROLES["viewer"]
