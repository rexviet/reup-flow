"""Protected runtime grants, atomically consumed once, and project-scoped sessions."""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from localization.review_bridge.repository import atomic_write, safe_path
from localization.storage.object_store import project_head_key

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class Unauthorized(Exception):
    pass


class Auth:
    def __init__(self, root: Path, *, now=time.time):
        self.root = root
        self.now = now
        if root.is_symlink():
            raise ValueError("runtime directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_uid != os.getuid():
            raise ValueError("runtime directory has another owner")
        root.chmod(0o700)

    def path(self, kind, token):
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            raise Unauthorized("Invalid session")
        digest = hashlib.sha256(token.encode()).hexdigest()
        return safe_path(self.root, f"{kind}-{digest}.json")

    def epoch_path(self, aweme, generation):
        project_head_key(aweme, generation)
        return safe_path(self.root, f"epoch-{aweme}-g{generation}.json")

    def issue(self, aweme, generation):
        token = secrets.token_urlsafe(32)
        epoch = secrets.token_urlsafe(32)
        atomic_write(self.epoch_path(aweme, generation), epoch.encode())
        atomic_write(
            self.path("grant", token),
            json.dumps(
                {
                    "aweme_id": aweme,
                    "generation": generation,
                    "epoch": epoch,
                    "expires": self.now() + 60,
                }
            ).encode(),
        )
        return token

    def valid(self, record):
        if self.now() >= record["expires"]:
            raise Unauthorized("Session expired; launch again")
        epoch = self.epoch_path(record["aweme_id"], record["generation"])
        if not epoch.exists() or epoch.read_text() != record["epoch"]:
            raise Unauthorized("Session replaced by another launch")
        return record

    def redeem(self, token):
        path = self.path("grant", token)
        consumed = safe_path(self.root, "consumed-" + secrets.token_hex(16))
        try:
            os.rename(path, consumed)
        except FileNotFoundError:
            raise Unauthorized("Launch grant expired or already used") from None
        try:
            record = self.valid(json.loads(consumed.read_bytes()))
        finally:
            consumed.unlink(missing_ok=True)
        cookie = secrets.token_urlsafe(32)
        record.update(expires=self.now() + 8 * 3600, csrf=secrets.token_urlsafe(32))
        atomic_write(self.path("session", cookie), json.dumps(record).encode())
        return cookie, record

    def session(self, cookie):
        path = self.path("session", cookie)
        try:
            record = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise Unauthorized("Launch the companion to sign in") from None
        return self.valid(record)
