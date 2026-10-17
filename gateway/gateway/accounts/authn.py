"""Who is calling the API: an operator key (Secrets Manager) or a user's own key."""

from dataclasses import dataclass

from gateway.accounts.keys import KeyService
from gateway.auth import KeyRing


@dataclass(frozen=True)
class Principal:
    kind: str  # "operator" or "user-key"
    id: str  # operator key name, or the user key's prefix
    owner: str  # "op:<name>" or the user key's owner ("user:<sub>", ...)
    label: str  # for logs: "operator:smoke", "key:3f9a1c2b (alice@example.com)"


class Authenticator:
    def __init__(self, keyring: KeyRing, keys: KeyService | None = None) -> None:
        self._keyring = keyring
        self._keys = keys

    async def identify(self, token: str) -> Principal | None:
        name = self._keyring.identify(token)
        if name is not None:
            return Principal("operator", name, f"op:{name}", f"operator:{name}")
        if self._keys is None or not token.startswith("zk_"):
            return None
        key = await self._keys.verify(token)
        if key is None:
            return None
        self._keys.record_use(key.prefix)
        return Principal("user-key", key.prefix, key.owner, f"key:{key.prefix} ({key.owner_label})")
