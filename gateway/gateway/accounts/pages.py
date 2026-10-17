"""HTML for the account pages: sign-in, invite links, and the admin pages.

Server-rendered, no script; every value is escaped. State-changing forms carry the
session's CSRF token in a hidden field (checked with the Origin by the portal).
"""

import html
from collections.abc import Sequence
from datetime import UTC, datetime

from gateway.accounts.invites import MAX_DAYS
from gateway.accounts.quota import human_bytes
from gateway.accounts.store import ApiKeyRecord, InviteRecord, LoginRecord, UserRecord


def when(moment: datetime | None) -> str:
    """`2026-10-17 10:30 UTC`, or `-` for never."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if moment else "-"


def _e(value: object) -> str:
    return html.escape(str(value))


def _csrf(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(token)}">'


def _post_button(action: str, label: str, csrf: str) -> str:
    return (
        f'<form method="post" action="{_e(action)}">{_csrf(csrf)}'
        f"<button>{_e(label)}</button></form>"
    )


def _table(headings: Sequence[str], rows: Sequence[str]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headings)
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


# ------------------------------------------------------------------------------ sign-in


def login_body(next_path: str, error: str | None, *, cognito: bool, key_login: bool) -> str:
    message = f'<p class="error" role="alert">{_e(error)}</p>' if error else ""
    parts = [f"<h1>Sign in</h1>{message}"]
    if cognito:
        parts.append(
            f'<p><a class="button" href="/docs/auth/start?next={_e(next_path)}">Sign in</a></p>'
            '<p class="muted">You need an invite link to have an account.</p>'
        )
    if key_login:
        form = (
            '<form method="post" action="/docs/login" class="login">'
            f'<input type="hidden" name="next" value="{_e(next_path)}">'
            '<label>API key <input type="password" name="api_key" '
            'autocomplete="current-password" required></label><button>Sign in with a key</button>'
            "</form>"
        )
        parts.append(
            f"<details><summary>Operators: sign in with an API key</summary>{form}</details>"
            if cognito
            else f"<p>Use the API key you call the zip services with.</p>{form}"
        )
    return "".join(parts)


def message_body(title: str, text: str, *, link: tuple[str, str] | None = None) -> str:
    action = f'<p><a href="{_e(link[0])}">{_e(link[1])}</a></p>' if link else ""
    return f"<h1>{_e(title)}</h1><p>{_e(text)}</p>{action}"


# ------------------------------------------------------------------------------ invite link


def invite_form(token: str, error: str | None = None) -> str:
    message = f'<p class="error" role="alert">{_e(error)}</p>' if error else ""
    return (
        "<h1>You're invited</h1>"
        "<p>Enter your email address. We'll create your account and Amazon Cognito will "
        "email you a temporary password; you choose your own when you first sign in.</p>"
        f'{message}<form method="post" action="/docs/invite/{_e(token)}" class="login">'
        '<label>Email <input type="email" name="email" autocomplete="email" maxlength="320" '
        "required autofocus></label><button>Create my account</button></form>"
        '<p class="muted">The link works once.</p>'
    )


# ------------------------------------------------------------------------------ admin


def admin_tabs(current: str) -> str:
    tabs = [("invites", "Invites"), ("users", "Users"), ("keys", "API keys")]
    links = " &middot; ".join(
        f"<strong>{label}</strong>"
        if slug == current
        else f'<a href="/docs/admin/{slug}">{label}</a>'
        for slug, label in tabs
    )
    return f'<p class="crumb">Admin: {links}</p>'


def admin_invites_body(
    invites: Sequence[InviteRecord],
    csrf: str,
    *,
    new_link: str | None,
    error: str | None,
    enabled: bool,
    now: datetime,
) -> str:
    parts = [admin_tabs("invites"), "<h1>Invites</h1>"]
    if not enabled:
        parts.append(
            '<p class="error">Sign-up is not configured (no Cognito user pool), so invite '
            "links cannot create accounts here.</p>"
        )
    parts.append(
        "<p>Create a one-time link and send it to whoever should get an account; they enter "
        "their own email when they open it.</p>"
    )
    if new_link:
        parts.append(
            '<div class="new-key" role="status"><p><strong>New invite link.</strong> Copy it '
            "now: it is shown only once.</p>"
            f"<pre><code>{_e(new_link)}</code></pre></div>"
        )
    if error:
        parts.append(f'<p class="error" role="alert">{_e(error)}</p>')
    parts.append(
        '<form method="post" action="/docs/admin/invites" class="inline-form">'
        f"{_csrf(csrf)}"
        '<label>Label <input name="label" maxlength="120" required '
        'placeholder="e.g. Recruiter, Acme"></label>'
        f'<label>Valid for (days) <input name="days" type="number" min="1" max="{MAX_DAYS}" '
        'value="7" required></label><button>Create link</button></form>'
    )
    rows = []
    for invite in invites:
        state = invite.state(now)
        action = (
            _post_button(f"/docs/admin/invites/{invite.id}/revoke", "Revoke", csrf)
            if state == "unused"
            else ""
        )
        rows.append(
            f"<tr><td>{_e(invite.label)}</td><td>{when(invite.created)}</td>"
            f"<td>{when(invite.expires)}</td>"
            f'<td class="{"ok" if state == "used" else "muted"}">{_e(state)}</td>'
            f"<td>{_e(invite.email or '-')}</td><td>{when(invite.used_at)}</td>"
            f"<td>{_e(invite.created_by)}</td><td>{action}</td></tr>"
        )
    if rows:
        parts.append(
            _table(
                ["Label", "Created", "Expires", "Status", "Used by", "Used", "Created by", ""],
                rows,
            )
        )
    else:
        parts.append('<p class="muted">No invites yet.</p>')
    return "".join(parts)


def admin_users_body(
    users: Sequence[UserRecord],
    invite_labels: dict[str, str],
    logins: Sequence[LoginRecord],
    csrf: str,
    error: str | None = None,
    usage: dict[str, int] | None = None,
    daily_quota: int = 0,
) -> str:
    parts = [admin_tabs("users"), "<h1>Users</h1>"]
    usage = usage or {}
    limit = f" of {human_bytes(daily_quota)}" if daily_quota else ""
    if error:
        parts.append(f'<p class="error" role="alert">{_e(error)}</p>')
    rows = []
    for user in users:
        action = (
            _post_button(f"/docs/admin/users/{user.sub}/disable", "Disable", csrf)
            if user.active
            else ""
        )
        status = "active" if user.active else f"disabled {when(user.disabled)}"
        rows.append(
            f"<tr><td>{_e(user.email)}</td>"
            f"<td>{_e(invite_labels.get(user.invite_id or '', '-'))}</td>"
            f"<td>{when(user.created)}</td><td>{when(user.last_login)}</td>"
            f"<td>{user.login_count}</td>"
            f"<td>{human_bytes(usage.get(f'user:{user.sub}', 0))}{limit}</td>"
            f'<td class="{"ok" if user.active else "muted"}">{_e(status)}</td>'
            f"<td>{action}</td></tr>"
        )
    parts.append(
        _table(
            ["Email", "Invite", "Created", "Last sign-in", "Sign-ins", "Today (UTC)", "Status", ""],
            rows,
        )
        if rows
        else '<p class="muted">No users yet: they appear after their first sign-in.</p>'
    )
    parts.append("<h2>Recent sign-ins</h2>")
    login_rows = [
        f"<tr><td>{when(login.at)}</td><td>{_e(login.email or login.sub)}</td>"
        f"<td>{_e(login.ip or '-')}</td><td>{_e(login.user_agent[:80] or '-')}</td></tr>"
        for login in logins
    ]
    parts.append(
        _table(["When", "User", "IP address", "Browser"], login_rows)
        if login_rows
        else '<p class="muted">No sign-ins yet.</p>'
    )
    parts.append('<p class="muted">Sign-in history is kept for 90 days.</p>')
    return "".join(parts)


def admin_keys_body(keys: Sequence[ApiKeyRecord], csrf: str) -> str:
    rows = []
    for key in keys:
        status = "active" if key.active else f"revoked {when(key.revoked)}"
        action = (
            _post_button(f"/docs/admin/keys/{key.prefix}/revoke", "Revoke", csrf)
            if key.active
            else ""
        )
        rows.append(
            f"<tr><td>{_e(key.owner_label)}</td><td>{_e(key.name)}</td>"
            f"<td><code>zk_{_e(key.prefix)}_...</code></td><td>{when(key.created)}</td>"
            f"<td>{when(key.last_used)}</td><td>{key.use_count}</td>"
            f'<td class="{"ok" if key.active else "muted"}">{_e(status)}</td>'
            f"<td>{action}</td></tr>"
        )
    return (
        admin_tabs("keys")
        + "<h1>API keys</h1><p>Every user key. Usage is recorded about once a minute.</p>"
        + (
            _table(["Owner", "Name", "Key", "Created", "Last used", "Requests", "Status", ""], rows)
            if rows
            else '<p class="muted">No user keys yet.</p>'
        )
    )
