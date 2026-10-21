from itertools import count


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}
        self._ids = count(1)

    def create(self, username: str) -> str:
        token = f"session-{next(self._ids)}"
        self._sessions[token] = username
        return token

    def lookup(self, token: str) -> str | None:
        return self._sessions.get(token)

    def revoke(self, token: str) -> None:
        pass
