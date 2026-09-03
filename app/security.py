from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretStore:
    """Small encryption helper for secrets kept in SQLite.

    The key is derived from BOT_TOKEN so the project does not need a second
    manually-managed secret just for the MVP. Protect the bot token and DB.
    """

    def __init__(self, bot_token: str):
        digest = hashlib.sha256(bot_token.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            return None
