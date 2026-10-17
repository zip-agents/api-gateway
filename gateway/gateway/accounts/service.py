"""Everything account-related the portal needs, built once from the settings (app.py)."""

from dataclasses import dataclass

from gateway.accounts.directory import CognitoDirectory, Directory
from gateway.accounts.invites import InviteService
from gateway.accounts.keys import KeyService
from gateway.accounts.oidc import CognitoOidc, FlowCookie
from gateway.accounts.quota import QuotaService
from gateway.accounts.store import SqlStore
from gateway.accounts.users import UserGate
from gateway.config import Settings


@dataclass
class Accounts:
    store: SqlStore
    keys: KeyService
    invites: InviteService
    users: UserGate
    quota: QuotaService
    flow: FlowCookie
    oidc: CognitoOidc | None  # None: Cognito not configured, API-key sign-in only
    directory: Directory | None

    @classmethod
    def build(cls, settings: Settings, store: SqlStore) -> "Accounts":
        oidc: CognitoOidc | None = None
        directory: Directory | None = None
        if settings.cognito is not None:
            oidc = CognitoOidc(settings.cognito)
            directory = CognitoDirectory(settings.cognito.user_pool_id, settings.cognito.region)
        return cls(
            store=store,
            keys=KeyService(store),
            invites=InviteService(store, directory),
            users=UserGate(store),
            quota=QuotaService(store, settings.user_daily_quota_bytes),
            flow=FlowCookie(settings.session_secret),
            oidc=oidc,
            directory=directory,
        )
