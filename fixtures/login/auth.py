"""Tiny example app with an intentional authentication bug."""

USERS = {"ada": "correct-horse"}


def authenticate(username: str, password: str) -> bool:
    # Bug: any nonempty password is accepted for an existing user.
    return username in USERS and bool(password)
