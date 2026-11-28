import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class TestAuthService(unittest.TestCase):
    def test_password_hash_and_verify(self):
        hashed = hash_password("secret123")
        self.assertTrue(verify_password("secret123", hashed))
        self.assertFalse(verify_password("wrong", hashed))
        self.assertFalse(verify_password("secret123", "malformed"))

    def test_jwt_create_and_decode(self):
        token = create_access_token(user_id=7, username="alice", role="dispatcher")
        payload = decode_access_token(token)
        self.assertEqual(payload["sub"], "7")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["role"], "dispatcher")

    def test_jwt_rejects_tampered_token(self):
        token = create_access_token(user_id=7, username="alice", role="dispatcher")
        tampered = token[:-4] + "xxxx"
        with self.assertRaises(ValueError):
            decode_access_token(tampered)


if __name__ == "__main__":
    unittest.main()
