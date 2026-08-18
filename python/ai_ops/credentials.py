"""Credential classes: what the controller holds so the sandbox never does.

The broker was built around one shape -- an API key in an operator-owned file.
But three of the four providers this rail targets (Grok, Codex/ChatGPT, Claude
Code) authenticate with OAuth sessions instead. Measured 2026-08-18, all three
store the same thing: a short-lived access token plus a long-lived refresh token
in a mode-600 file the controller can read. So OAuth is not a special case per
provider; it is one additional credential CLASS.

    api-key   an operator-installed key            -> Bearer <key>
    oauth     a CLI's own logged-in session file   -> Bearer <access token>

Either way the sandbox receives a placeholder and a loopback URL, and the broker
attaches the real value upstream. The containment story is unchanged.

THE INVARIANT, and it is stronger than "do not forward it": the refresh token is
NEVER READ. Not into a variable, not into a log, not into a header. An access
token expires in about an hour; a refresh token is the whole subscription, and
the cheapest way to guarantee it cannot leak is to never load it. The extractors
below deliberately pull only the access token and the expiry, and a test asserts
the refresh token's actual bytes appear nowhere in what this module returns.

Consequence, accepted knowingly: an expired session refuses with an instruction
to re-login rather than silently refreshing. Controller-side refresh is a real
improvement and the structure is here for it, but it must be built against a
measured token endpoint per provider -- writing three refresh flows from
documentation is exactly the mistake this project keeps paying for.

THE ONE EXCEPTION, and it is scoped and named: a provider whose CLI validates
its own session LOCALLY (measured: Grok) cannot be handed a placeholder, so its
ACCESS token must be inside the sandbox -- the fallback tier. There,
load_sandbox_session reads the whole session file and strips the refresh token
in the controller before writing it into the sandbox. So even in the fallback
tier the refresh token never enters the sandbox and the rail never persists it;
what changes is that the ~1h access token does. Egress containment is unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import Refuse

# Treat a token expiring within this window as already expired: a job that
# starts with 20 seconds left will outlive its own credential mid-flight, and a
# clear refusal now beats a 401 from inside the sandbox later.
EXPIRY_SKEW_S = 120


@dataclass
class Credential:
    """A usable credential plus how it was obtained. Never the refresh token."""

    token: str
    cls: str
    source: str
    expires_at: float | None = None
    # Headers the UPSTREAM requires that are derived from the real credential,
    # so they must be injected controller-side alongside it. Codex needs
    # ChatGPT-Account-ID, and the CLI derives it from its own token's claims --
    # which in the full tier is a PLACEHOLDER, so the sandbox would send the
    # wrong account id unless the broker overrides it.
    extra_headers: dict = field(default_factory=dict)

    def authorization(self) -> str:
        return f"Bearer {self.token}"

    def seconds_remaining(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()

    def describe(self) -> str:
        """Safe for logs and errors: never includes the token."""
        if self.expires_at is None:
            return f"{self.cls} from {self.source}"
        left = int(self.seconds_remaining() or 0)
        return f"{self.cls} from {self.source} ({left}s remaining)"


def _require_private(path: str) -> None:
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise Refuse(
            f"credential file {path} is group/world accessible "
            f"(mode {st.st_mode & 0o777:o}); chmod 600 it"
        )


def _iso_to_epoch(value: str) -> float | None:
    """Parse the ISO-8601 stamps these CLIs write.

    Grok and Codex emit nanosecond precision with a Z suffix
    (`2026-08-18T07:46:34.587567321Z`), which fromisoformat rejects on older
    Pythons and which no amount of guessing fixes -- so truncate the fraction to
    microseconds and normalise the zone explicitly.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        text = f"{head}.{digits[:6]}{rest}"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _jwt_expiry(token: str) -> float | None:
    """Read `exp` out of a JWT payload without verifying the signature.

    Verification is the issuer's job and we are not the audience; this is only a
    local "is it worth sending" check. Codex stores no expiry field of its own,
    so its access token's own claim is the only measurement available.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


# --- per-provider extractors, each written from an inspected real file --------
#
# Only the access token and the expiry are pulled out. `refresh_token` is
# present in every one of these files and is deliberately never touched.


def _extract_grok(doc: dict[str, Any]) -> tuple[str, float | None]:
    """`{"<issuer>::<uuid>": {"key": <jwt>, "expires_at": <iso>, ...}}`"""
    for key, entry in doc.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key")
        if isinstance(token, str) and token:
            exp = entry.get("expires_at")
            return token, (_iso_to_epoch(exp) if isinstance(exp, str) else None)
    raise Refuse("grok auth file has no session entry with a key")


def codex_account_id(token: str) -> str | None:
    """The chatgpt_account_id claim, read from the access token.

    Measured: the Codex CLI puts this in a ChatGPT-Account-ID header and derives
    it from whatever token it holds. Under the full tier that is a placeholder,
    so the broker must replace the header using the REAL token's claim.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        acct = auth.get("chatgpt_account_id")
        if isinstance(acct, str) and acct:
            return acct
    return None


def _extract_codex(doc: dict[str, Any]) -> tuple[str, float | None]:
    """`{"tokens": {"access_token": <jwt>, ...}, "OPENAI_API_KEY": null}`

    An installed API key wins if present: it is the cleaner credential and the
    file offers it first.
    """
    api_key = doc.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        return api_key, None
    tokens = doc.get("tokens")
    if isinstance(tokens, dict):
        token = tokens.get("access_token")
        if isinstance(token, str) and token:
            return token, _jwt_expiry(token)
    raise Refuse("codex auth file has neither OPENAI_API_KEY nor tokens.access_token")


def _extract_claude(doc: dict[str, Any]) -> tuple[str, float | None]:
    """`{"claudeAiOauth": {"accessToken": ..., "expiresAt": <epoch ms>}}`"""
    oauth = doc.get("claudeAiOauth")
    if isinstance(oauth, dict):
        token = oauth.get("accessToken")
        if isinstance(token, str) and token:
            exp = oauth.get("expiresAt")
            # Milliseconds, not seconds -- measured. Treating it as seconds puts
            # the expiry in 1970 and refuses every single job.
            return token, (float(exp) / 1000.0 if isinstance(exp, (int, float)) else None)
    raise Refuse("claude credentials file has no claudeAiOauth.accessToken")


_EXTRACTORS: dict[str, Callable[[dict[str, Any]], tuple[str, float | None]]] = {
    "grok-oidc": _extract_grok,
    "codex-tokens": _extract_codex,
    "claude-oauth": _extract_claude,
}


def _has_refresh_token(obj: Any) -> bool:
    """Whether the session could be refreshed BY ITS OWN CLI.

    Checks only for the presence of such a field, at any depth -- deliberately
    without reading the value, which is the invariant this module exists to keep.
    """
    if isinstance(obj, dict):
        return any(
            ("refresh" in k.lower() and bool(v)) or _has_refresh_token(v)
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return any(_has_refresh_token(x) for x in obj)
    return False


def refresh_via_own_cli(
    provider_id: str, prec: dict[str, Any], provider_argv: list[str], adapter
) -> bool:
    """Ask the provider's own CLI to refresh its session -- on a COPY.

    Returns True if the real session was replaced with a fresher one.

    THE COPY IS NOT AN OPTIMISATION, IT IS THE SAFETY PROPERTY. The first version
    of this bind-mounted the real credential directory read-write so the CLI
    could write the refreshed token back. Measured consequence: presented with a
    session it judged invalid, the Grok CLI did not refresh it -- it DELETED
    auth.json. That is a destroyed login, and in production an actually-expired
    token would hit exactly that path. A credential store is not ours to hand to
    a process that may decide to reset it.

    So the CLI operates on a throwaway copy. If it produces a valid, fresher
    session there, that file is atomically installed over the real one (with a
    backup alongside). If it wipes the copy, errors, or produces something no
    better, the real store is never touched.

    agent-ops still never reads the refresh token: the CLI that owns the
    credential performs its own refresh. That is what makes one mechanism serve
    every provider instead of a hand-written OAuth flow per vendor.
    """
    import shutil
    import subprocess
    import tempfile

    from . import sandbox

    argv = adapter.refresh_argv(list(provider_argv)) if hasattr(adapter, "refresh_argv") else None
    if not argv:
        return False
    path = os.path.expanduser(prec.get("auth_file") or "")
    if not path or not os.path.isfile(path):
        return False
    auth_dir = os.path.dirname(path)
    name = os.path.basename(auth_dir)
    rel = os.path.relpath(path, auth_dir)

    # Serialise refreshes for this provider. These refresh tokens ROTATE: a
    # successful refresh invalidates the one that was used. Two jobs refreshing
    # at once therefore race to burn the same rotation, and the loser is left
    # holding a token the issuer has already retired -- a broken login caused by
    # nothing but concurrency. Measured the hard way: a refresh whose result was
    # discarded instead of persisted killed the session it was trying to save.
    import fcntl

    lock_path = path + ".aiops-refresh.lock"
    home = tempfile.mkdtemp(prefix="aiops-refresh-")
    lock_fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another job is already refreshing. Do not race it; that job will
            # write the new session and this one re-reads on its next attempt.
            return False
        # ONLY the session file, into an otherwise-empty directory. Copying the
        # whole credential directory drags in lock files, caches and local state
        # that change how the CLI behaves -- measured: a full-directory copy made
        # the Grok CLI discard the session instead of refreshing it, while a bare
        # directory holding just auth.json refreshed cleanly. Less is both safer
        # and, here, the only thing that works.
        work = os.path.join(home, name)
        os.makedirs(work, exist_ok=True)
        shutil.copy2(path, os.path.join(work, rel))
        try:
            full = sandbox.build_credential_refresh_argv(
                auth_dir=work, synth_home=home, provider_argv=argv
            )
            subprocess.run(
                full,
                env={"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "TERM": "dumb"},
                capture_output=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            return False

        refreshed = os.path.join(work, rel)
        if not os.path.isfile(refreshed):
            return False  # the CLI wiped its copy; the real store is untouched
        try:
            with open(refreshed, encoding="utf-8") as fh:
                doc = json.load(fh)
            extractor = _EXTRACTORS[prec["auth_format"]]
            _token, exp = extractor(doc)
        except Exception:
            return False
        # Only accept a session that is actually USABLE and actually newer.
        if exp is None or exp - time.time() <= EXPIRY_SKEW_S:
            return False

        shutil.copy2(path, path + ".aiops-backup")
        tmp = path + ".aiops-new"
        shutil.copy2(refreshed, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # atomic
        return True
    finally:
        shutil.rmtree(home, ignore_errors=True)
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def load_oauth_credential(
    provider_id: str, prec: dict[str, Any], on_expired=None
) -> Credential:
    """`on_expired` is called once if the token is stale; if it returns True the
    file is re-read. That is how an expired-but-refreshable session becomes a
    working job instead of a refusal the operator has to fix by hand."""
    auth_file = prec.get("auth_file")
    fmt = prec.get("auth_format")
    if not auth_file or not fmt:
        raise Refuse(
            f"provider '{provider_id}' is credential_class oauth but the registry "
            "gives no auth_file/auth_format"
        )
    extractor = _EXTRACTORS.get(fmt)
    if extractor is None:
        raise Refuse(
            f"unknown auth_format {fmt!r} for provider '{provider_id}' "
            f"(known: {sorted(_EXTRACTORS)})"
        )
    path = os.path.expanduser(auth_file)
    if not os.path.isfile(path):
        raise Refuse(
            f"provider '{provider_id}' has no session at {path} -- log in with its own CLI first"
        )
    _require_private(path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refuse(f"unreadable session file {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refuse(f"session file {path} is not a JSON object")

    token, expires_at = extractor(doc)
    if (
        on_expired is not None
        and expires_at is not None
        and expires_at - time.time() <= EXPIRY_SKEW_S
        and on_expired()
    ):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        token, expires_at = extractor(doc)
    cred = Credential(token=token, cls="oauth", source=path, expires_at=expires_at)
    if fmt == "codex-tokens":
        acct = codex_account_id(token)
        if acct:
            cred.extra_headers["chatgpt-account-id"] = acct
    remaining = cred.seconds_remaining()
    if remaining is not None and remaining <= EXPIRY_SKEW_S:
        # These access tokens are short-lived (about an hour) and the vendor CLI
        # refreshes its own file whenever it runs. So the usual cause is not a
        # lost login -- it is simply that the CLI has not been used for a while.
        # Say that, because "re-login" sends the operator to a browser flow they
        # almost never need.
        has_refresh = _has_refresh_token(doc)
        fix = (
            f"run any {provider_id} command (e.g. `{provider_id} models`) to let its own "
            "CLI refresh the session, then retry"
            if has_refresh
            else f"log in again with the {provider_id} CLI"
        )
        raise Refuse(
            f"provider '{provider_id}' access token expired {-int(remaining)}s ago "
            f"({path}): {fix}. agent-ops does not refresh it itself -- it never "
            "reads the refresh token, which is the whole subscription"
        )
    return cred


def strip_refresh_tokens(obj: Any) -> Any:
    """Deep copy of a session document with every refresh-token field removed.

    Recursive and name-based: any key whose lowercased name contains "refresh"
    is dropped, at any depth. Used only for the in-sandbox fallback tier, where
    the provider's CLI validates its session locally and therefore the access
    token must be inside the sandbox. The refresh token must NOT be: it is the
    whole subscription, and this is what guarantees it is never written there.
    """
    if isinstance(obj, dict):
        return {k: strip_refresh_tokens(v) for k, v in obj.items() if "refresh" not in k.lower()}
    if isinstance(obj, list):
        return [strip_refresh_tokens(x) for x in obj]
    return obj


def load_sandbox_session(provider_id: str, prec: dict[str, Any]) -> dict[str, Any]:
    """The provider's own session file, refresh token stripped, for a fallback
    tier that must place the access token inside the sandbox.

    This is a DELIBERATE weakening relative to the brokered tiers, used only for
    a provider whose CLI validates its session locally (measured: Grok). The
    access token -- good for about an hour -- ends up on disk in the sandbox
    home. The refresh token is stripped here in the controller and never written.
    Egress containment is unchanged: --unshare-net and the broker's path
    allowlist still mean the token can be USED for the job but has no channel out
    except the one brokered upstream.
    """
    path = os.path.expanduser(prec.get("auth_file") or "")
    if not path or not os.path.isfile(path):
        raise Refuse(
            f"provider '{provider_id}' has no session at {path or '(unset auth_file)'}; "
            "log in with its own CLI first"
        )
    _require_private(path)
    # Reuse load_oauth_credential purely to run the same expiry/skew refusal the
    # brokered path enforces, so a fallback job cannot start on a dead session.
    load_oauth_credential(provider_id, prec)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return strip_refresh_tokens(doc)


def load_credential(provider_id: str, prec: dict[str, Any], **kwargs) -> Credential | None:
    """Resolve a provider's credential by its registry-declared class.

    Returns None only for a configured api-key provider with no key installed --
    the fail-closed path job.run_job already refuses on. Every other failure is a
    Refuse with the reason, because "no credential" and "your session expired"
    need different actions from the operator.
    """
    cls = (prec.get("credential_class") or "api-key").strip().lower()
    if cls == "oauth":
        return load_oauth_credential(provider_id, prec, on_expired=kwargs.get("on_expired"))
    if cls != "api-key":
        raise Refuse(
            f"provider '{provider_id}' has unknown credential_class {cls!r} "
            "(known: api-key, oauth)"
        )
    from .provider import credential_path, load_provider_credential

    name = prec.get("credential") or provider_id
    value = load_provider_credential(name)
    if not value:
        return None
    return Credential(token=value, cls="api-key", source=credential_path(name))
