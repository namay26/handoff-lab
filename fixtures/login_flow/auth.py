USERS = {
    "ada": "correct-horse",
    "grace": "compiler",
}


def authenticate(username: str, password: str) -> bool:
    return username in USERS and bool(password)
