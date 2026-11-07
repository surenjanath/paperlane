"""Save files straight to Google Drive or Microsoft OneDrive / SharePoint.

Set up once on the server (an OAuth app with your provider):
    PAPERLANE_GOOGLE_CLIENT_ID, PAPERLANE_GOOGLE_CLIENT_SECRET              scope: drive.file
    PAPERLANE_MS_CLIENT_ID, PAPERLANE_MS_CLIENT_SECRET, PAPERLANE_MS_TENANT   scopes: Files.ReadWrite.All offline_access
Redirect URI to register: <SITE_URL>/account/connections/oauth/<google|microsoft>/callback/
Then each user clicks Connect under Account › Connections; the refresh token is kept in Connection.data.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.conf import settings
from django.core import signing

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.file"
MS_SCOPE = "offline_access Files.ReadWrite.All User.Read"
SALT = "paperlane.oauth"


class DriveError(Exception):
    pass


def configured(provider):
    if provider == "google":
        return bool(os.environ.get("PAPERLANE_GOOGLE_CLIENT_ID") and os.environ.get("PAPERLANE_GOOGLE_CLIENT_SECRET"))
    if provider == "microsoft":
        return bool(os.environ.get("PAPERLANE_MS_CLIENT_ID") and os.environ.get("PAPERLANE_MS_CLIENT_SECRET"))
    return False


def _ms_base():
    return f"https://login.microsoftonline.com/{os.environ.get('PAPERLANE_MS_TENANT', 'common')}/oauth2/v2.0"


def redirect_uri(provider):
    return settings.SITE_URL.rstrip("/") + f"/account/connections/oauth/{provider}/callback/"


def auth_url(provider, user, workspace):
    state = signing.dumps({"u": user.pk, "w": workspace.pk, "p": provider}, salt=SALT)
    if provider == "google":
        q = {"client_id": os.environ["PAPERLANE_GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri(provider), "response_type": "code",
             "scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent", "state": state}
        return f"{GOOGLE_AUTH}?{urllib.parse.urlencode(q)}"
    q = {"client_id": os.environ["PAPERLANE_MS_CLIENT_ID"], "redirect_uri": redirect_uri(provider), "response_type": "code",
         "scope": MS_SCOPE, "state": state, "response_mode": "query"}
    return f"{_ms_base()}/authorize?{urllib.parse.urlencode(q)}"


def read_state(state, max_age=900):
    return signing.loads(state, salt=SALT, max_age=max_age)


def _post_form(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise DriveError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"can't reach {url.split('/')[2]}: {exc}")


def _client(provider):
    if provider == "google":
        return {"client_id": os.environ.get("PAPERLANE_GOOGLE_CLIENT_ID", ""), "client_secret": os.environ.get("PAPERLANE_GOOGLE_CLIENT_SECRET", "")}
    return {"client_id": os.environ.get("PAPERLANE_MS_CLIENT_ID", ""), "client_secret": os.environ.get("PAPERLANE_MS_CLIENT_SECRET", "")}


def exchange(provider, code):
    """Tokens for an authorization code: {"access_token", "refresh_token", "expires_at"}."""
    url = GOOGLE_TOKEN if provider == "google" else f"{_ms_base()}/token"
    data = _post_form(url, {**_client(provider), "code": code, "grant_type": "authorization_code",
                            "redirect_uri": redirect_uri(provider), **({} if provider == "google" else {"scope": MS_SCOPE})})
    if not data.get("refresh_token"):
        raise DriveError("The provider didn't return a refresh token. Remove Paperlane's access in your account and connect again.")
    return {"access_token": data.get("access_token", ""), "refresh_token": data["refresh_token"],
            "expires_at": time.time() + int(data.get("expires_in", 3000)) - 60}


def access_token(conn):
    """A valid access token for the connection, refreshed (and saved) when it has expired."""
    tok = conn.data or {}
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise DriveError(f"“{conn.label}” isn't connected. Connect it again under Account › Connections.")
    provider = "google" if conn.kind == "google" else "microsoft"
    url = GOOGLE_TOKEN if provider == "google" else f"{_ms_base()}/token"
    data = _post_form(url, {**_client(provider), "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
                            **({} if provider == "google" else {"scope": MS_SCOPE})})
    tok = {**tok, "access_token": data.get("access_token", ""), "expires_at": time.time() + int(data.get("expires_in", 3000)) - 60}
    if data.get("refresh_token"):
        tok["refresh_token"] = data["refresh_token"]
    conn.data = tok
    conn.save(update_fields=["data"])
    return tok["access_token"]


def _request(method, url, token, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers={"authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise DriveError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"can't reach {url.split('/')[2]}: {exc}")


def _clean(part):
    return "".join(c for c in str(part) if c not in '\\/:*?"<>|#%').strip()[:120] or "Untitled"


def _google_folder(token, path, root=None):
    parent = root or "root"
    for part in [_clean(p) for p in path.split("/") if p.strip()]:
        q = (f"name = '{part.replace(chr(39), chr(92) + chr(39))}' and mimeType = 'application/vnd.google-apps.folder' "
             f"and '{parent}' in parents and trashed = false")
        found = _request("GET", "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}), token)
        if found.get("files"):
            parent = found["files"][0]["id"]
        else:
            made = _request("POST", "https://www.googleapis.com/drive/v3/files?fields=id", token,
                            json.dumps({"name": part, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}).encode(),
                            {"content-type": "application/json"})
            parent = made["id"]
    return parent


def upload(conn, folder, filename, content, mime="application/pdf", drive=""):
    """Upload ``content`` into ``folder`` (a path like "Claims/2026/CLM-7"). Returns a link or the file id."""
    token = access_token(conn)
    filename = _clean(filename)
    if conn.kind == "google":
        parent = _google_folder(token, folder, root=drive or None)
        boundary = uuid.uuid4().hex
        meta = json.dumps({"name": filename, "parents": [parent]})
        body = (f"--{boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\ncontent-type: {mime}\r\n\r\n").encode() + content + f"\r\n--{boundary}--".encode()
        made = _request("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink", token,
                        body, {"content-type": f"multipart/related; boundary={boundary}"})
        return made.get("webViewLink") or made.get("id", "")
    path = "/".join(_clean(p) for p in folder.split("/") if p.strip())
    target = urllib.parse.quote(f"{path}/{filename}" if path else filename)
    base = f"https://graph.microsoft.com/v1.0/drives/{drive}" if drive else "https://graph.microsoft.com/v1.0/me/drive"
    if len(content) <= 4 * 1024 * 1024:
        made = _request("PUT", f"{base}/root:/{target}:/content", token, content, {"content-type": mime})  # creates folders
        return made.get("webUrl") or made.get("id", "")
    session = _request("POST", f"{base}/root:/{target}:/createUploadSession", token,
                       json.dumps({"item": {"@microsoft.graph.conflictBehavior": "rename"}}).encode(), {"content-type": "application/json"})
    url, chunk, made = session["uploadUrl"], 5 * 1024 * 1024, {}  # larger files go up in 5 MB pieces
    for start in range(0, len(content), chunk):
        piece = content[start:start + chunk]
        req = urllib.request.Request(url, data=piece, method="PUT", headers={
            "content-range": f"bytes {start}-{start + len(piece) - 1}/{len(content)}", "content-length": str(len(piece))})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                made = json.loads(raw) if raw else made
        except urllib.error.HTTPError as exc:
            raise DriveError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    return made.get("webUrl") or made.get("id", "")
