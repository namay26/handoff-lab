import unittest

from app import LoginApp


class LoginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = LoginApp()

    def test_valid_login_creates_session(self) -> None:
        token = self.app.login("ada", "correct-horse")
        self.assertIsInstance(token, str)
        self.assertEqual(self.app.profile(token), {"username": "ada"})

    def test_wrong_password_cannot_create_session(self) -> None:
        self.assertIsNone(self.app.login("ada", "wrong-password"))

    def test_unknown_user_cannot_create_session(self) -> None:
        self.assertIsNone(self.app.login("unknown", "correct-horse"))

    def test_missing_token_cannot_access_profile(self) -> None:
        self.assertIsNone(self.app.profile("missing-token"))

    def test_sessions_are_distinct(self) -> None:
        first = self.app.login("ada", "correct-horse")
        second = self.app.login("ada", "correct-horse")
        self.assertNotEqual(first, second)
        self.assertEqual(self.app.profile(first), {"username": "ada"})
        self.assertEqual(self.app.profile(second), {"username": "ada"})

    def test_sessions_keep_users_separate(self) -> None:
        ada = self.app.login("ada", "correct-horse")
        grace = self.app.login("grace", "compiler")
        self.assertEqual(self.app.profile(ada), {"username": "ada"})
        self.assertEqual(self.app.profile(grace), {"username": "grace"})

    def test_logout_invalidates_only_its_session(self) -> None:
        ada = self.app.login("ada", "correct-horse")
        grace = self.app.login("grace", "compiler")
        self.app.logout(ada)
        self.assertIsNone(self.app.profile(ada))
        self.assertEqual(self.app.profile(grace), {"username": "grace"})


if __name__ == "__main__":
    unittest.main()
