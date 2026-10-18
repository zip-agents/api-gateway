"""Who is calling the API: an operator key (Secrets Manager) or a user's own key.

A user key is only as good as its owner: a key made in an operator session stops working
when that operator key is removed from the configuration, and a user's keys stop working
when the user is disabled, deleted or disabled in Cognito (checked with the same cached
lookup as the portal, users.py), not only when each key is revoked.
"""

from dataclasses import dataclass

from gateway.accounts.keys import KeyService
from gateway.accounts.users import UserGate
from gateway.auth import KeyRing


@dataclass(frozen=True)
class Principal:
    kind: str  # "operator" or "user-key"
    id: str  # operator key name, or the user key's prefix
    owner: str  # "op:<name>" or the user key's owner ("user:<sub>", ...)
    label: str  # for logs: "operator:smoke", "key:3f9a1c2b (alice@example.com)"


class Authenticator:
    def __init__(
        self, keyring: KeyRing, keys: KeyService | None = None, users: UserGate | None = None
    ) -> None:
        self._keyring = keyring
        self._keys = keys
        self._users = users

    async def identify(self, token: str) -> Principal | None:
        name = self._keyring.identify(token)
        if name is not None:
            return Principal("operator", name, f"op:{name}", f"operator:{name}")
        if self._keys is None or not token.startswith("zk_"):
            return None
        key = await self._keys.verify(token)
        if key is None or not await self._owner_active(key.owner):
            return None
        self._keys.record_use(key.prefix)
        return Principal("user-key", key.prefix, key.owner, f"key:{key.prefix} ({key.owner_label})")

    async def _owner_active(self, owner: str) -> bool:
        if owner.startswith("op:"):
            return owner[3:] in self._keyring  # the operator key still exists
        if owner.startswith("user:"):
            return self._users is None or (await self._users.state(owner[5:])).active
        return False
