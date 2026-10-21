from auth import authenticate
from sessions import SessionStore


class LoginApp:
    def __init__(self) -> None:
        self.sessions = SessionStore()

    def login(self, username: str, password: str) -> str | None:
        if not authenticate(username, password):
            return None
        return self.sessions.create(username)

    def profile(self, token: str) -> dict[str, str] | None:
        return {"username": self.sessions.lookup(token) or ""}

    def logout(self, token: str) -> None:
        self.sessions.revoke(token)
