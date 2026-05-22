"""Corporate-jargon translator backed by the Claude API.

Authenticates with the OAuth token written by the ``claude`` CLI into
``~/.claude/.credentials.json`` (``claudeAiOauth.accessToken``). On a 401 the
token is refreshed in place using the stored ``refreshToken`` and the new
tokens are written back to the credentials file.
"""

import json
import os
import time

import requests

def _credentials_path() -> str:
    """Locate ~/.claude/.credentials.json, honoring SUDO_USER when run via sudo.

    Without this, ``sudo python3 main.py`` resolves ``~`` to ``/root`` and the
    credentials (which live in the invoking user's home) are not found.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            import pwd  # POSIX only
            home = pwd.getpwnam(sudo_user).pw_dir
            return os.path.join(home, ".claude", ".credentials.json")
        except Exception:
            pass
    return os.path.expanduser("~/.claude/.credentials.json")


CREDENTIALS_PATH = _credentials_path()

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 500
REQUEST_TIMEOUT_S = 30

SYSTEM_PROMPT = (
    "You are a corporate-speak translator. Rewrite the user's text as verbose, "
    "polished corporate jargon. Lean heavily on business buzzwords and "
    "management cliches such as: synergize, leverage, circle back, bandwidth, "
    "deliverables, strategic alignment, pivot, paradigm shift, low-hanging "
    "fruit, move the needle, value-add, action items, touch base, holistic, "
    "core competencies, and operationalize. Preserve the original meaning but "
    "make it sound like an executive wrote it. Respond with ONLY the rewritten "
    "text -- no preamble, no quotes, no explanation."
)


class TranslationError(RuntimeError):
    """Raised when the text could not be translated."""


def _load_credentials() -> dict:
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_credentials(creds: dict) -> None:
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
    os.replace(tmp, CREDENTIALS_PATH)
    # If running via sudo, restore the original user's ownership so later
    # non-root use of the credentials still works.
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            os.chown(CREDENTIALS_PATH, int(sudo_uid), int(sudo_gid))
        except Exception:
            pass


def _access_token(creds: dict) -> str:
    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise TranslationError(
            "No accessToken in %s -- run `claude` to log in." % CREDENTIALS_PATH
        )
    return token


def _refresh_token(creds: dict) -> dict:
    """Exchange the refresh token for a fresh access token, persist, return creds."""
    oauth = creds.get("claudeAiOauth", {})
    refresh = oauth.get("refreshToken")
    if not refresh:
        raise TranslationError("No refreshToken available; re-run `claude` to log in.")

    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": OAUTH_CLIENT_ID,
        },
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise TranslationError(
            "Token refresh failed (%s): %s" % (resp.status_code, resp.text[:200])
        )

    data = resp.json()
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    creds["claudeAiOauth"] = oauth
    _save_credentials(creds)
    return creds


def _request(text: str, token: str) -> requests.Response:
    return requests.post(
        MESSAGES_URL,
        headers={
            "Authorization": "Bearer %s" % token,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
        },
        timeout=REQUEST_TIMEOUT_S,
    )


def _extract_text(resp: requests.Response) -> str:
    data = resp.json()
    blocks = data.get("content", [])
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    out = "".join(parts).strip()
    if not out:
        raise TranslationError("Empty response from Claude API.")
    return out


def translate(text: str) -> str:
    """Rewrite ``text`` as corporate jargon. Raises ``TranslationError`` on failure."""
    text = (text or "").strip()
    if not text:
        raise TranslationError("Nothing to translate (clipboard was empty).")

    creds = _load_credentials()
    resp = _request(text, _access_token(creds))

    if resp.status_code == 401:
        # Access token likely expired -- refresh once and retry.
        creds = _refresh_token(creds)
        resp = _request(text, _access_token(creds))

    if resp.status_code != 200:
        raise TranslationError(
            "Claude API error (%s): %s" % (resp.status_code, resp.text[:200])
        )

    return _extract_text(resp)


if __name__ == "__main__":
    import sys

    sample = " ".join(sys.argv[1:]) or "Let's talk later about the project."
    print(translate(sample))
