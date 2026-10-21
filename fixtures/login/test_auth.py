import unittest

from auth import authenticate


class AuthenticationTests(unittest.TestCase):
    def test_valid_credentials(self):
        self.assertTrue(authenticate("ada", "correct-horse"))

    def test_wrong_password(self):
        self.assertFalse(authenticate("ada", "wrong-password"))

    def test_unknown_user(self):
        self.assertFalse(authenticate("nobody", "correct-horse"))


if __name__ == "__main__":
    unittest.main()
