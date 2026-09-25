from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os
import sys
import pathlib
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import base64
import urllib.parse
import json

from app.mapping import (
    JiraConfig, AzureConfig, MappingConfig, SyncRequest, DateFilter,
    default_mapping_config,
    resolve_state_generic, resolve_priority_generic, resolve_assignee_email_generic,
)

# The debug logging throughout this module prints unicode arrows/checkmarks
# (e.g. "→", "✓"). When stdout isn't a UTF-8 terminal (redirected to a log
# file, Windows console, etc.) those prints raise UnicodeEncodeError, which
# escapes try/except blocks and turns into an opaque 500 response.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
app = FastAPI()

_BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR    = os.path.join(_BASE_DIR, "static")
_FRONTEND_DIST = os.path.join(_BASE_DIR, "frontend", "dist")
os.makedirs(_STATIC_DIR, exist_ok=True)

# UI route — serve the React build; fall back to old index.html during dev if dist absent
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_ui():
    dist_html = pathlib.Path(_FRONTEND_DIST) / "index.html"
    if dist_html.exists():
        return HTMLResponse(content=dist_html.read_text(encoding="utf-8"))
    legacy = pathlib.Path(_BASE_DIR) / "templates" / "index.html"
    return HTMLResponse(content=legacy.read_text(encoding="utf-8"))

# Vite-built JS/CSS assets
_dist_assets = os.path.join(_FRONTEND_DIST, "assets")
if os.path.isdir(_dist_assets):
    app.mount("/assets", StaticFiles(directory=_dist_assets), name="assets")

# Logo / favicon served at /static/* (referenced from React components and index.html)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ============================================
# DYNAMIC AZURE STATE RESOLUTION
# ============================================

_azure_state_cache: dict = {}   # keyed by "org/project"


def fetch_azure_states_for_project(azure_org: str, azure_project: str, auth_header: str) -> dict:
    headers = {"Authorization": auth_header, "Content-Type": "application/json"}
    result = {}
    try:
        types_resp = requests.get(
            f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitemtypes?api-version=7.1",
            headers=headers, timeout=10
        )
        if types_resp.status_code == 200:
            all_wit_names = [t.get("name") for t in types_resp.json().get("value", []) if t.get("name")]
            print(f"  [STATE CACHE] Work item types in project: {all_wit_names}")
        else:
            all_wit_names = ["User Story", "Task", "Bug", "Epic", "Feature", "Issue", "Test Case"]
            print(f"  [STATE CACHE] Could not fetch types ({types_resp.status_code}), using fallback list")
    except Exception as e:
        all_wit_names = ["User Story", "Task", "Bug", "Epic", "Feature", "Issue"]
        print(f"  [STATE CACHE] ERROR fetching types: {e} — using fallback list")

    for wit in all_wit_names:
        encoded = urllib.parse.quote(wit)
        try:
            resp = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitemtypes/{encoded}/states?api-version=7.1",
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                states = [s.get("name") for s in resp.json().get("value", []) if s.get("name")]
                result[wit] = states
                print(f"  [STATE CACHE] '{wit}' valid states: {states}")
            else:
                result[wit] = []
        except Exception as e:
            print(f"  [STATE CACHE] ERROR for '{wit}': {e}")
            result[wit] = []
    return result


def get_or_load_state_cache(azure_org: str, azure_project: str, auth_header: str) -> dict:
    global _azure_state_cache
    key = f"{azure_org}/{azure_project}"
    if key not in _azure_state_cache:
        print(f"\n  [STATE CACHE] First sync — fetching valid states from Azure project '{azure_project}'...")
        _azure_state_cache[key] = fetch_azure_states_for_project(azure_org, azure_project, auth_header)
    return _azure_state_cache[key]


def _pick_state(jira_status: str, available_states: list, preferences_map: dict, wit_label: str) -> str:
    lower_to_actual = {s.lower(): s for s in available_states}
    candidates = preferences_map.get(jira_status, [])
    for c in candidates:
        actual = lower_to_actual.get(c.lower())
        if actual:
            print(f"    [STATE MAP] {wit_label}: JIRA '{jira_status}' → Azure '{actual}'")
            return actual
    for fallback_hint in ["to do", "new", "proposed", "backlog", "open", "active"]:
        actual = lower_to_actual.get(fallback_hint)
        if actual:
            print(f"    [STATE MAP] {wit_label}: JIRA '{jira_status}' → fallback '{actual}' (no preference matched from {available_states})")
            return actual
    last_resort = available_states[0] if available_states else "To Do"
    print(f"    [STATE MAP] {wit_label}: JIRA '{jira_status}' → last-resort '{last_resort}'")
    return last_resort


def resolve_story_state(jira_status: str, available_states: list, assignee_email: str = None) -> str:
    """
    Jira -> Azure DevOps state mapping for User Story:
      To Do  + no assignee  -> Backlog
      To Do  + assignee     -> To Do
      In Progress           -> Doing
      DevOps/QA Requested/QA In Progress -> In Review
      Pending Dev           -> Blocked
      UAT / Done            -> Done
    """
    normalised   = (jira_status or "").strip().lower()
    has_assignee = bool(assignee_email and assignee_email.strip())

    print(f"    [STATE MAP] resolve_story_state: jira_status='{jira_status}' "
          f"normalised='{normalised}' has_assignee={has_assignee} available={available_states}")

    # Rule 1a: To Do + NO assignee -> Backlog
    if normalised == "to do" and not has_assignee:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Backlog", "New", "Proposed", "To Do"]}, "User Story")
        print(f"    [STATE MAP] User Story: 'To Do' (no assignee) -> '{result}' (Backlog)")
        return result

    # Rule 1b: To Do + assignee -> To Do
    if normalised == "to do" and has_assignee:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["To Do", "New", "Proposed", "Backlog"]}, "User Story")
        print(f"    [STATE MAP] User Story: 'To Do' (has assignee) -> '{result}' (To Do)")
        return result

    # Rule 2: In Progress -> Doing
    if normalised == "in progress":
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Doing", "Active", "In Progress", "In Development", "In Dev"]},
                             "User Story")
        print(f"    [STATE MAP] User Story: 'In Progress' -> '{result}' (Doing)")
        return result

    # Rule 3: DevOps / QA Requested / QA In Progress -> In Review
    if any(kw in normalised for kw in ["devops", "qa requested", "qa in progress", "qa"]):
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["In Review", "Review", "Active", "In Progress"]},
                             "User Story")
        print(f"    [STATE MAP] User Story: '{jira_status}' (QA/DevOps) -> '{result}' (In Review)")
        return result

    # Rule 4: Pending Dev -> Blocked
    if "pending dev" in normalised:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Blocked", "On Hold", "To Do", "New"]},
                             "User Story")
        print(f"    [STATE MAP] User Story: 'Pending Dev' -> '{result}' (Blocked)")
        return result

    # Rule 5: UAT / Done -> Done
    if normalised in ("done", "uat", "closed", "resolved", "finished", "completed"):
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Done", "Closed", "Completed", "Resolved", "Finished"]},
                             "User Story")
        print(f"    [STATE MAP] User Story: '{jira_status}' (Done/UAT) -> '{result}' (Done)")
        return result

    # Fallback
    print(f"    [STATE MAP] User Story: '{jira_status}' did not match any rule -- using generic fallback")
    prefs = {
        "To Do":   ["To Do", "New", "Proposed", "Backlog", "Ready", "Open", "Pending"],
        "Blocked": ["Blocked", "On Hold", "To Do", "New", "Proposed", "Backlog"],
    }
    return _pick_state(jira_status, available_states, prefs, "User Story")


def resolve_task_state(jira_status: str, available_states: list, assignee_email: str = None) -> str:
    """
    Jira -> Azure DevOps state mapping for Task (mirrors User Story rules):
      To Do  + no assignee  -> Backlog
      To Do  + assignee     -> To Do
      In Progress           -> In Progress (Tasks rarely have "Doing")
      DevOps/QA Requested/QA In Progress -> In Review
      Pending Dev           -> Blocked
      UAT / Done            -> Done
    """
    normalised   = (jira_status or "").strip().lower()
    has_assignee = bool(assignee_email and assignee_email.strip())

    print(f"    [STATE MAP] resolve_task_state: jira_status='{jira_status}' "
          f"normalised='{normalised}' has_assignee={has_assignee} available={available_states}")

    # Rule 1a: To Do + NO assignee -> Backlog
    if normalised == "to do" and not has_assignee:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Backlog", "New", "Proposed", "To Do"]}, "Task")
        print(f"    [STATE MAP] Task: 'To Do' (no assignee) -> '{result}' (Backlog)")
        return result

    # Rule 1b: To Do + assignee -> To Do
    if normalised == "to do" and has_assignee:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["To Do", "New", "Proposed", "Backlog"]}, "Task")
        print(f"    [STATE MAP] Task: 'To Do' (has assignee) -> '{result}' (To Do)")
        return result

    # Rule 2: In Progress -> In Progress / Doing
    if normalised == "in progress":
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["In Progress", "Doing", "Active", "In Development", "In Dev"]},
                             "Task")
        print(f"    [STATE MAP] Task: 'In Progress' -> '{result}'")
        return result

    # Rule 3: DevOps / QA Requested / QA In Progress -> In Review
    if any(kw in normalised for kw in ["devops", "qa requested", "qa in progress", "qa"]):
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["In Review", "Review", "In Progress", "Active"]},
                             "Task")
        print(f"    [STATE MAP] Task: '{jira_status}' (QA/DevOps) -> '{result}' (In Review)")
        return result

    # Rule 4: Pending Dev -> Blocked
    if "pending dev" in normalised:
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Blocked", "On Hold", "To Do", "New"]},
                             "Task")
        print(f"    [STATE MAP] Task: 'Pending Dev' -> '{result}' (Blocked)")
        return result

    # Rule 5: UAT / Done -> Done
    if normalised in ("done", "uat", "closed", "resolved", "finished", "completed"):
        result = _pick_state(jira_status, available_states,
                             {jira_status: ["Done", "Closed", "Completed", "Resolved", "Finished"]},
                             "Task")
        print(f"    [STATE MAP] Task: '{jira_status}' (Done/UAT) -> '{result}' (Done)")
        return result

    # Fallback
    print(f"    [STATE MAP] Task: '{jira_status}' did not match any rule -- using generic fallback")
    prefs = {
        "To Do":   ["To Do", "New", "Proposed", "Not Started", "Backlog", "Open"],
        "Blocked": ["Blocked", "On Hold", "To Do", "New", "Open"],
    }
    return _pick_state(jira_status, available_states, prefs, "Task")


# ============================================
# JIRA CHANGELOG — IN PROGRESS DATE RESOLVER
#
# The Azure DevOps StartDate for a ticket should reflect the PLANNED start date
# stored in Jira's customfield_10015. This takes priority over all other dates.
#
# Falls back to: startdate field → creation date
# ============================================

def get_inprogress_date_from_changelog(issue_key: str, jira_base_url: str,
                                        jira_email: str, jira_token: str) -> str | None:
    """
    Fetch the Jira issue changelog and return the ISO datetime string for when
    the issue FIRST transitioned to an 'In Progress' category status.
    Returns None if the issue has never been moved to In Progress.

    Jira changelog is paginated and returned in ascending order (oldest first),
    so the first match we encounter is the earliest transition.
    """
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json"}

    start_at      = 0
    max_results   = 100
    all_histories = []

    print(f"  [CHANGELOG] Fetching changelog for {issue_key}...")

    while True:
        try:
            resp = requests.get(
                f"{jira_base_url}/rest/api/3/issue/{issue_key}/changelog"
                f"?startAt={start_at}&maxResults={max_results}",
                headers=headers, auth=auth, timeout=10
            )
        except Exception as e:
            print(f"  [CHANGELOG] Network error fetching changelog for {issue_key}: {e}")
            return None

        if resp.status_code != 200:
            print(f"  [CHANGELOG] Could not fetch changelog for {issue_key}: HTTP {resp.status_code}")
            return None

        data   = resp.json()
        values = data.get("values", [])
        all_histories.extend(values)

        total_available = data.get("total", 0)
        print(f"  [CHANGELOG] Fetched {len(all_histories)}/{total_available} history entries for {issue_key}")

        if len(all_histories) >= total_available or len(values) == 0:
            break
        start_at += max_results

    # Walk entries in order (ascending = oldest first) and find the FIRST
    # status transition whose destination name contains "in progress"
    first_inprogress_date = None

    for history in all_histories:
        created = history.get("created")  # ISO datetime of this change event
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue

            to_string = (item.get("toString") or "").strip()
            to_lower  = to_string.lower()

            if "in progress" in to_lower or "in development" in to_lower or "in dev" in to_lower:
                from_string = (item.get("fromString") or "").strip()
                print(f"  [CHANGELOG] {issue_key}: status transition at {created}: "
                      f"'{from_string}' → '{to_string}'  ← IN PROGRESS match")

                if first_inprogress_date is None:
                    first_inprogress_date = created

    if first_inprogress_date:
        print(f"  [CHANGELOG] {issue_key}: ✓ First In Progress date = {first_inprogress_date}")
    else:
        print(f"  [CHANGELOG] {issue_key}: ticket has never transitioned to In Progress")

    return first_inprogress_date


# ============================================
# JIRA CHANGELOG — QA DATE RESOLVER
#
# The Azure DevOps DueDate / TargetDate (Planned Completion Date) for a ticket
# should reflect WHEN work was first moved to QA — i.e. the first time the Jira
# ticket transitioned into a QA/Testing/Review category status.
#
# If the ticket has NEVER been moved to QA, the end date is left NULL (not set).
# Do NOT fall back to duedate or today's date — an unset end date is intentional.
#
# Matched status names (case-insensitive, substring):
#   "qa", "in qa", "testing", "in testing", "ready for qa",
#   "in review", "review", "ready for review"
# ============================================

def get_qa_date_from_changelog(issue_key: str, jira_base_url: str,
                                jira_email: str, jira_token: str) -> str | None:
    """
    Fetch the Jira issue changelog and return the ISO datetime string for when
    the issue FIRST transitioned to a QA/Testing/Review status.
    Returns None if the issue has never been moved to QA.

    Jira changelog is paginated and returned in ascending order (oldest first),
    so the first match we encounter is the earliest QA transition.
    """
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json"}

    start_at      = 0
    max_results   = 100
    all_histories = []

    print(f"  [CHANGELOG QA] Fetching changelog for {issue_key}...")

    while True:
        try:
            resp = requests.get(
                f"{jira_base_url}/rest/api/3/issue/{issue_key}/changelog"
                f"?startAt={start_at}&maxResults={max_results}",
                headers=headers, auth=auth, timeout=10
            )
        except Exception as e:
            print(f"  [CHANGELOG QA] Network error fetching changelog for {issue_key}: {e}")
            return None

        if resp.status_code != 200:
            print(f"  [CHANGELOG QA] Could not fetch changelog for {issue_key}: HTTP {resp.status_code}")
            return None

        data   = resp.json()
        values = data.get("values", [])
        all_histories.extend(values)

        total_available = data.get("total", 0)
        print(f"  [CHANGELOG QA] Fetched {len(all_histories)}/{total_available} history entries for {issue_key}")

        if len(all_histories) >= total_available or len(values) == 0:
            break
        start_at += max_results

    # QA status name patterns to match (case-insensitive substring matching)
    QA_PATTERNS = [
        "in qa", "qa", "testing", "in testing", "ready for qa",
        "in review", "review", "ready for review", "under review",
    ]

    first_qa_date = None

    for history in all_histories:
        created = history.get("created")  # ISO datetime of this change event
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue

            to_string = (item.get("toString") or "").strip()
            to_lower  = to_string.lower()

            if any(pattern in to_lower for pattern in QA_PATTERNS):
                from_string = (item.get("fromString") or "").strip()
                print(f"  [CHANGELOG QA] {issue_key}: status transition at {created}: "
                      f"'{from_string}' → '{to_string}'  ← QA match")

                if first_qa_date is None:
                    # This is the earliest one because changelog is ascending
                    first_qa_date = created
                    # Don't break — log all matches but keep the first

    if first_qa_date:
        print(f"  [CHANGELOG QA] {issue_key}: ✓ First QA date = {first_qa_date}")
    else:
        print(f"  [CHANGELOG QA] {issue_key}: ticket has never transitioned to QA — endDate will be NULL")

    return first_qa_date


# ============================================
# JIRA / ATLASSIAN MEDIA RESOLVER
# ============================================

# Cache: jira_issue_key → {mediaUUID: presigned_download_url}
_issue_media_url_cache: dict = {}


def build_issue_media_map(issue_key: str, jira_base_url: str,
                          jira_email: str, jira_token: str) -> dict:
    if issue_key in _issue_media_url_cache:
        print(f"      [MEDIA MAP] Using cached media map for {issue_key} ({len(_issue_media_url_cache[issue_key])} entries)")
        return _issue_media_url_cache[issue_key]

    print(f"      [MEDIA MAP] Building media UUID→URL map for issue {issue_key}...")
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json"}
    media_map = {}

    try:
        issue_resp = requests.get(
            f"{jira_base_url}/rest/api/3/issue/{issue_key}?fields=attachment",
            headers=headers, auth=auth, timeout=10
        )
        print(f"      [MEDIA MAP] Issue fetch status: {issue_resp.status_code}")
        if issue_resp.status_code != 200:
            print(f"      [MEDIA MAP] Could not fetch issue attachments: {issue_resp.text[:200]}")
            return {}
        attachments = issue_resp.json().get("fields", {}).get("attachment", [])
        print(f"      [MEDIA MAP] Found {len(attachments)} attachments on issue {issue_key}")
    except Exception as e:
        print(f"      [MEDIA MAP] Error fetching issue: {e}")
        return {}

    import re
    uuid_pattern = re.compile(
        r'/file/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/',
        re.IGNORECASE
    )

    for att in attachments:
        att_id       = att.get("id")
        att_filename = att.get("filename", "")
        att_mime     = att.get("mimeType", "")
        print(f"      [MEDIA MAP] Attachment id={att_id} filename='{att_filename}' mime='{att_mime}'")

        try:
            content_resp = requests.get(
                f"{jira_base_url}/rest/api/3/attachment/content/{att_id}",
                headers={"Accept": "*/*"},
                auth=auth,
                timeout=20,
                allow_redirects=True
            )
            final_url = content_resp.url
            print(f"      [MEDIA MAP] Followed redirect → {final_url[:120]}...")

            match = uuid_pattern.search(final_url)
            if match:
                media_uuid = match.group(1)
                media_map[media_uuid] = {
                    "presigned_url": final_url,
                    "filename":      att_filename,
                    "mime_type":     att_mime,
                    "att_id":        att_id,
                    "content":       content_resp.content if content_resp.status_code == 200 else None,
                    "content_type":  content_resp.headers.get("Content-Type", att_mime),
                }
                print(f"      [MEDIA MAP] ✓ Mapped mediaUUID={media_uuid} → filename='{att_filename}'")
            else:
                print(f"      [MEDIA MAP] Could not extract media UUID from redirect URL: {final_url[:120]}")

        except Exception as e:
            print(f"      [MEDIA MAP] Error following redirect for att_id={att_id}: {e}")

    print(f"      [MEDIA MAP] Built map with {len(media_map)} media UUID entries for {issue_key}")
    _issue_media_url_cache[issue_key] = media_map
    return media_map


def download_media_as_base64_from_map(media_uuid: str, media_map: dict) -> tuple:
    entry = media_map.get(media_uuid)
    if not entry:
        print(f"      [MEDIA DL] media UUID '{media_uuid}' not found in issue media map")
        return "", media_uuid

    filename = entry.get("filename", media_uuid)
    mime     = (entry.get("content_type") or entry.get("mime_type") or "").split(";")[0].strip()

    body = entry.get("content")
    if body and len(body) > 0:
        if b"<!DOCTYPE" in body[:100] or b"<html" in body[:100]:
            print(f"      [MEDIA DL] Got HTML page for '{filename}' — trying presigned URL directly")
            body = None

    if not body:
        presigned_url = entry.get("presigned_url", "")
        if not presigned_url:
            print(f"      [MEDIA DL] No presigned URL for '{filename}'")
            return "", filename
        try:
            print(f"      [MEDIA DL] Downloading from presigned URL for '{filename}'...")
            resp = requests.get(presigned_url, timeout=30)
            print(f"      [MEDIA DL] Presigned download status={resp.status_code} bytes={len(resp.content)}")
            if resp.status_code == 200 and len(resp.content) > 0:
                body = resp.content
                mime = resp.headers.get("Content-Type", mime).split(";")[0].strip()
            else:
                print(f"      [MEDIA DL] Presigned download failed: {resp.status_code}")
                return "", filename
        except Exception as e:
            print(f"      [MEDIA DL] Exception downloading presigned URL: {e}")
            return "", filename

    if not mime or mime == "application/octet-stream":
        if body[:8] == b'\x89PNG\r\n\x1a\n':
            mime = "image/png"
        elif body[:2] == b'\xff\xd8':
            mime = "image/jpeg"
        elif body[:6] in (b'GIF87a', b'GIF89a'):
            mime = "image/gif"
        elif body[:4] == b'RIFF' and body[8:12] == b'WEBP':
            mime = "image/webp"
        else:
            mime = "image/png"
        print(f"      [MEDIA DL] Guessed mime from magic bytes: {mime}")

    encoded  = base64.b64encode(body).decode("utf-8")
    data_uri = f"data:{mime};base64,{encoded}"
    print(f"      [MEDIA DL] ✓ base64 ready for '{filename}' mime='{mime}' encoded_len={len(encoded)}")
    return data_uri, filename


def get_atlassian_media_token(jira_base_url: str, jira_email: str, jira_token: str) -> str:
    global _media_token_cache
    cached = _media_token_cache.get(jira_base_url)
    if cached and cached.get("expires", 0) > datetime.now().timestamp() + 60:
        print(f"      [MEDIA TOKEN] Using cached token (expires in >{int(cached['expires'] - datetime.now().timestamp())}s)")
        return cached["token"]

    print(f"      [MEDIA TOKEN] Fetching new media token from Jira...")

    url = f"{jira_base_url}/rest/api/3/serverInfo"
    try:
        si = requests.get(url, headers={"Accept": "application/json"}, auth=(jira_email, jira_token), timeout=10)
        print(f"      [MEDIA TOKEN] serverInfo status: {si.status_code}")
    except Exception as e:
        print(f"      [MEDIA TOKEN] serverInfo error: {e}")

    token_url = f"{jira_base_url}/gateway/api/media/token/user/client"
    try:
        print(f"      [MEDIA TOKEN] Trying: {token_url}")
        resp = requests.post(
            token_url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"fileIds": []},
            auth=(jira_email, jira_token),
            timeout=10
        )
        print(f"      [MEDIA TOKEN] Response status: {resp.status_code}")
        print(f"      [MEDIA TOKEN] Response body: {resp.text[:500]}")
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token") or data.get("access_token") or data.get("clientId", "")
            if token:
                expires_at = datetime.now().timestamp() + data.get("expiresIn", 300)
                _media_token_cache[jira_base_url] = {"token": token, "expires": expires_at}
                print(f"      [MEDIA TOKEN] ✓ Got token (len={len(token)})")
                return token
    except Exception as e:
        print(f"      [MEDIA TOKEN] Error on gateway endpoint: {e}")

    token_url2 = f"{jira_base_url}/rest/api/3/attachment/upload"
    try:
        print(f"      [MEDIA TOKEN] Trying upload endpoint: {token_url2}")
        resp2 = requests.get(
            token_url2,
            headers={"Accept": "application/json"},
            auth=(jira_email, jira_token),
            timeout=10
        )
        print(f"      [MEDIA TOKEN] Upload endpoint status: {resp2.status_code}")
    except Exception as e:
        print(f"      [MEDIA TOKEN] Upload endpoint error: {e}")

    print(f"      [MEDIA TOKEN] Could not obtain media token — will try direct download")
    return ""


def download_media_file_as_base64(file_id: str, collection: str, jira_base_url: str,
                                   jira_email: str, jira_token: str) -> tuple:
    print(f"      [MEDIA DL] Attempting download for fileId='{file_id}' collection='{collection}'")

    auth = (jira_email, jira_token)

    strategies = [
        {
            "url":  f"{jira_base_url}/gateway/api/media/api/file/{file_id}/binary",
            "headers": {"Accept": "*/*"},
            "auth": auth,
            "label": "gateway proxy /binary"
        },
        {
            "url":  f"{jira_base_url}/rest/api/3/attachment/content/{file_id}",
            "headers": {"Accept": "*/*"},
            "auth": auth,
            "label": "REST attachment/content"
        },
        {
            "url":  f"{jira_base_url}/gateway/api/media/api/file/{file_id}/image",
            "headers": {"Accept": "*/*"},
            "auth": auth,
            "label": "gateway proxy /image"
        },
        {
            "url":  f"https://api.media.atlassian.com/file/{file_id}/binary",
            "headers": {"Accept": "*/*", "X-Client-Name": "jira-sync"},
            "auth": auth,
            "label": "direct media API"
        },
    ]

    for strategy in strategies:
        url    = strategy["url"]
        label  = strategy["label"]
        hdrs   = strategy["headers"]
        s_auth = strategy["auth"]
        try:
            print(f"      [MEDIA DL] Trying [{label}]: {url}")
            resp = requests.get(url, headers=hdrs, auth=s_auth, timeout=30, allow_redirects=True)
            print(f"      [MEDIA DL] [{label}] status={resp.status_code} "
                  f"Content-Type={resp.headers.get('Content-Type','?')} "
                  f"Content-Length={resp.headers.get('Content-Length','?')}")

            if resp.status_code == 200:
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                body = resp.content
                print(f"      [MEDIA DL] [{label}] Downloaded {len(body)} bytes")

                if len(body) == 0:
                    print(f"      [MEDIA DL] [{label}] 0 bytes — skipping")
                    continue

                if content_type.startswith("text/") or b"<!DOCTYPE" in body[:100] or b"<html" in body[:100]:
                    print(f"      [MEDIA DL] [{label}] Got HTML/text response — not an image, skipping")
                    continue

                if not content_type or content_type == "application/octet-stream":
                    if body[:8] == b'\x89PNG\r\n\x1a\n':
                        content_type = "image/png"
                    elif body[:2] == b'\xff\xd8':
                        content_type = "image/jpeg"
                    elif body[:6] in (b'GIF87a', b'GIF89a'):
                        content_type = "image/gif"
                    elif body[:4] == b'RIFF' and body[8:12] == b'WEBP':
                        content_type = "image/webp"
                    else:
                        content_type = "image/png"
                    print(f"      [MEDIA DL] [{label}] Guessed mime from magic bytes: {content_type}")

                encoded  = base64.b64encode(body).decode("utf-8")
                data_uri = f"data:{content_type};base64,{encoded}"
                print(f"      [MEDIA DL] ✓ [{label}] Success! mime='{content_type}' base64_len={len(encoded)}")
                return data_uri, file_id

            elif resp.status_code in (301, 302, 307, 308):
                redirect_url = resp.headers.get("Location", "")
                print(f"      [MEDIA DL] [{label}] Redirect to: {redirect_url}")
            elif resp.status_code == 401:
                print(f"      [MEDIA DL] [{label}] 401 Unauthorized — wrong credentials for this endpoint")
            elif resp.status_code == 403:
                print(f"      [MEDIA DL] [{label}] 403 Forbidden — no access")
            elif resp.status_code == 404:
                print(f"      [MEDIA DL] [{label}] 404 Not Found")
            else:
                print(f"      [MEDIA DL] [{label}] Unexpected {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            print(f"      [MEDIA DL] [{label}] Exception: {e}")

    print(f"      [MEDIA DL] ✗ All strategies failed for fileId='{file_id}'")
    return "", file_id


def fetch_jira_attachment_url(attachment_id: str, jira_base_url: str, jira_email: str, jira_token: str):
    try:
        url = f"{jira_base_url}/rest/api/3/attachment/{attachment_id}"
        print(f"      [ATTACHMENT] Trying legacy attachment API for ID={attachment_id}")
        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            auth=(jira_email, jira_token),
            timeout=10
        )
        print(f"      [ATTACHMENT] Legacy API response status: {response.status_code}")
        if response.status_code == 200:
            data        = response.json()
            content_url = data.get("content", "")
            mime_type   = data.get("mimeType", "")
            filename    = data.get("filename", attachment_id)
            print(f"      [ATTACHMENT] Legacy hit: filename='{filename}' mimeType='{mime_type}' url='{content_url}'")
            return content_url, mime_type, filename
        else:
            print(f"      [ATTACHMENT] Legacy API 404/error — this is a Media API file ID, not an attachment ID")
    except Exception as e:
        print(f"      [ATTACHMENT] Legacy API error: {e}")
    return "", "", attachment_id


def download_jira_attachment_as_base64(content_url: str, mime_type: str, jira_email: str, jira_token: str) -> str:
    try:
        print(f"      [ATTACHMENT] Downloading from resolved URL: {content_url}")
        resp = requests.get(content_url, auth=(jira_email, jira_token), timeout=30)
        print(f"      [ATTACHMENT] Download status: {resp.status_code} bytes={len(resp.content)}")
        if resp.status_code == 200 and len(resp.content) > 0:
            actual_mime = resp.headers.get("Content-Type", mime_type).split(";")[0].strip()
            if not actual_mime or actual_mime == "application/octet-stream":
                actual_mime = mime_type
            encoded = base64.b64encode(resp.content).decode("utf-8")
            print(f"      [ATTACHMENT] ✓ base64 ready. mime='{actual_mime}' len={len(encoded)}")
            return f"data:{actual_mime};base64,{encoded}"
        else:
            print(f"      [ATTACHMENT] Download failed or empty: {resp.status_code}")
    except Exception as e:
        print(f"      [ATTACHMENT] Download error: {e}")
    return ""


# ============================================
# ADF → HTML CONVERTER
# ============================================

def adf_marks_open(marks: list) -> str:
    html = ""
    for mark in marks:
        mark_type = mark.get("type", "")
        attrs     = mark.get("attrs", {})
        if mark_type == "strong":
            html += "<strong>"
        elif mark_type == "em":
            html += "<em>"
        elif mark_type == "underline":
            html += "<u>"
        elif mark_type == "strike":
            html += "<s>"
        elif mark_type == "code":
            html += "<code style='background:#f4f5f7;padding:2px 4px;border-radius:3px;font-family:monospace;'>"
        elif mark_type == "link":
            href  = attrs.get("href", "#")
            title = attrs.get("title", "")
            html += f'<a href="{href}"' + (f' title="{title}"' if title else "") + ">"
        elif mark_type == "textColor":
            color = attrs.get("color", "")
            html += f'<span style="color:{color};">'
        elif mark_type == "backgroundColor":
            color = attrs.get("color", "")
            html += f'<span style="background-color:{color};">'
        elif mark_type == "subsup":
            tag   = "sub" if attrs.get("type") == "sub" else "sup"
            html += f"<{tag}>"
    return html


def adf_marks_close(marks: list) -> str:
    html = ""
    for mark in reversed(marks):
        mark_type = mark.get("type", "")
        attrs     = mark.get("attrs", {})
        if mark_type == "strong":
            html += "</strong>"
        elif mark_type == "em":
            html += "</em>"
        elif mark_type == "underline":
            html += "</u>"
        elif mark_type == "strike":
            html += "</s>"
        elif mark_type == "code":
            html += "</code>"
        elif mark_type == "link":
            html += "</a>"
        elif mark_type == "textColor":
            html += "</span>"
        elif mark_type == "backgroundColor":
            html += "</span>"
        elif mark_type == "subsup":
            tag   = "sub" if attrs.get("type") == "sub" else "sup"
            html += f"</{tag}>"
    return html


def adf_node_to_html(node: dict, list_type: str = None, jira_creds: dict = None) -> str:
    if not node or not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    attrs     = node.get("attrs", {})
    content   = node.get("content", [])
    marks     = node.get("marks", [])
    text      = node.get("text", "")

    def r(child):
        return adf_node_to_html(child, jira_creds=jira_creds)

    if node_type == "text":
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return adf_marks_open(marks) + safe + adf_marks_close(marks)

    if node_type == "hardBreak":
        return "<br/>"

    if node_type == "mention":
        display = attrs.get("text") or attrs.get("id", "")
        return f'<span style="color:#0052CC;">@{display}</span>'

    if node_type == "emoji":
        shortname = attrs.get("shortName", attrs.get("text", ""))
        return shortname

    if node_type == "inlineCard":
        url = attrs.get("url", "")
        return f'<a href="{url}">{url}</a>'

    if node_type == "date":
        timestamp = attrs.get("timestamp", "")
        try:
            dt = datetime.utcfromtimestamp(int(timestamp) / 1000).strftime("%Y-%m-%d")
        except Exception:
            dt = timestamp
        return f'<time datetime="{dt}">{dt}</time>'

    if node_type == "status":
        label     = attrs.get("text", "")
        color     = attrs.get("color", "neutral")
        color_map = {
            "neutral": "#97a0af", "purple": "#6554c0", "blue": "#0052cc",
            "red": "#de350b", "yellow": "#ff991f", "green": "#36b37e",
        }
        bg = color_map.get(color, "#97a0af")
        return f'<span style="background:{bg};color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;">{label}</span>'

    if node_type == "mediaSingle":
        inner  = "".join(r(c) for c in content)
        layout = attrs.get("layout", "center")
        align  = "center" if layout in ("center", "wide", "full-width") else "left"
        return f'<div style="text-align:{align};margin:8px 0;">{inner}</div>'

    if node_type == "media":
        media_type    = attrs.get("type", "")
        url           = attrs.get("url", "")
        width         = attrs.get("width", "")
        height        = attrs.get("height", "")
        attachment_id = attrs.get("id", "")

        print(f"      [MEDIA NODE] type='{media_type}' id='{attachment_id}' url='{url}' width={width} height={height}")

        style = ""
        if width:
            style += f"max-width:{width}px;"
        if height:
            style += f"max-height:{height}px;"

        if media_type == "external" and url:
            print(f"      [MEDIA NODE] External image — embedding directly: {url}")
            return f'<img src="{url}" style="{style}max-width:100%;" alt="image"/>'

        elif media_type == "file":
            print(f"      [MEDIA NODE] File media node — id='{attachment_id}'")

            if not attachment_id:
                print(f"      [MEDIA NODE] No attachment ID — cannot resolve")
                return '<p>📎 <em>Attachment (no ID)</em></p>'

            if not jira_creds:
                print(f"      [MEDIA NODE] No jira_creds — cannot download")
                return f'<p>📎 <em>Attachment: {attachment_id}</em></p>'

            issue_key = jira_creds.get("issue_key", "")
            if not issue_key:
                print(f"      [MEDIA NODE] No issue_key in jira_creds — cannot build media map")
                return f'<p>📎 <em>Attachment: {attachment_id[:8]}...</em></p>'

            media_map = build_issue_media_map(
                issue_key,
                jira_creds.get("base_url", ""),
                jira_creds.get("email", ""),
                jira_creds.get("token", "")
            )

            data_uri, filename = download_media_as_base64_from_map(attachment_id, media_map)

            if data_uri:
                print(f"      [MEDIA NODE] ✓ Embedding as base64 <img>")
                return f'<img src="{data_uri}" style="{style}max-width:100%;" alt="{filename}"/>'

            print(f"      [MEDIA NODE] ✗ Could not resolve image — showing label")
            return f'<p>📎 <em>Image (view in Jira)</em></p>'

        else:
            print(f"      [MEDIA NODE] Unknown media type='{media_type}' — skipping")
            return ""

    if node_type == "mediaGroup":
        return "".join(r(c) for c in content)

    if node_type == "mediaInline":
        url           = attrs.get("url", "")
        attachment_id = attrs.get("id", "")
        print(f"      [MEDIA INLINE] url='{url}' id='{attachment_id}'")

        if url:
            return f'<img src="{url}" style="max-width:100%;vertical-align:middle;" alt="inline image"/>'

        if attachment_id and jira_creds:
            issue_key = jira_creds.get("issue_key", "")
            if issue_key:
                media_map = build_issue_media_map(
                    issue_key,
                    jira_creds.get("base_url", ""),
                    jira_creds.get("email", ""),
                    jira_creds.get("token", "")
                )
                data_uri, filename = download_media_as_base64_from_map(attachment_id, media_map)
                if data_uri:
                    print(f"      [MEDIA INLINE] ✓ Rendering inline as base64 <img>")
                    return f'<img src="{data_uri}" style="max-width:100%;vertical-align:middle;" alt="{filename}"/>'
        return ""

    if node_type == "doc":
        return "".join(r(c) for c in content)

    if node_type == "paragraph":
        inner = "".join(r(c) for c in content)
        return f"<p>{inner}</p>"

    if node_type == "heading":
        level = attrs.get("level", 1)
        inner = "".join(r(c) for c in content)
        return f"<h{level}>{inner}</h{level}>"

    if node_type == "bulletList":
        items = "".join(r(c) for c in content)
        return f"<ul>{items}</ul>"

    if node_type == "orderedList":
        order = attrs.get("order", 1)
        items = "".join(r(c) for c in content)
        return f'<ol start="{order}">{items}</ol>'

    if node_type == "listItem":
        inner = "".join(r(c) for c in content)
        return f"<li>{inner}</li>"

    if node_type == "blockquote":
        inner = "".join(r(c) for c in content)
        return f'<blockquote style="border-left:3px solid #ccc;margin:4px 0;padding:4px 8px;color:#666;">{inner}</blockquote>'

    if node_type == "codeBlock":
        language = attrs.get("language", "")
        inner    = "".join(
            (n.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             if n.get("type") == "text" else "")
            for n in content
        )
        return (
            f'<pre style="background:#f4f5f7;padding:12px;border-radius:3px;overflow:auto;">'
            f'<code>{inner}</code>'
            f'</pre>'
        )

    if node_type == "rule":
        return "<hr/>"

    if node_type == "expand":
        title = attrs.get("title", "Details")
        inner = "".join(r(c) for c in content)
        return f'<details><summary><strong>{title}</strong></summary>{inner}</details>'

    if node_type == "nestedExpand":
        title = attrs.get("title", "Details")
        inner = "".join(r(c) for c in content)
        return f'<details><summary>{title}</summary>{inner}</details>'

    if node_type == "panel":
        panel_type = attrs.get("panelType", "info")
        color_map  = {"info": "#deebff", "note": "#deebff", "warning": "#fffae6", "error": "#ffebe6", "success": "#e3fcef"}
        border_map = {"info": "#0052cc", "note": "#0052cc", "warning": "#ff991f", "error": "#de350b",  "success": "#36b37e"}
        icon_map   = {"info": "ℹ️",      "note": "📝",       "warning": "⚠️",      "error": "❌",        "success": "✅"}
        bg     = color_map.get(panel_type, "#f4f5f7")
        border = border_map.get(panel_type, "#97a0af")
        icon   = icon_map.get(panel_type, "")
        inner  = "".join(r(c) for c in content)
        return (
            f'<div style="background:{bg};border-left:4px solid {border};'
            f'padding:8px 12px;margin:8px 0;border-radius:0 3px 3px 0;">'
            f'{icon} {inner}</div>'
        )

    if node_type == "table":
        inner = "".join(r(c) for c in content)
        return f'<table style="border-collapse:collapse;width:100%;margin:8px 0;">{inner}</table>'

    if node_type == "tableRow":
        inner = "".join(r(c) for c in content)
        return f"<tr>{inner}</tr>"

    if node_type == "tableHeader":
        colspan = attrs.get("colspan", 1)
        rowspan = attrs.get("rowspan", 1)
        inner   = "".join(r(c) for c in content)
        return (
            f'<th colspan="{colspan}" rowspan="{rowspan}" '
            f'style="border:1px solid #dfe1e6;padding:6px 8px;background:#f4f5f7;text-align:left;">'
            f'{inner}</th>'
        )

    if node_type == "tableCell":
        colspan = attrs.get("colspan", 1)
        rowspan = attrs.get("rowspan", 1)
        bg      = attrs.get("background", "")
        style   = "border:1px solid #dfe1e6;padding:6px 8px;"
        if bg:
            style += f"background:{bg};"
        inner = "".join(r(c) for c in content)
        return f'<td colspan="{colspan}" rowspan="{rowspan}" style="{style}">{inner}</td>'

    if content:
        return "".join(r(c) for c in content)

    return ""


def adf_to_html(description_field, jira_creds: dict = None) -> str:
    if not description_field:
        return ""
    if isinstance(description_field, str):
        safe = description_field.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<p>{safe}</p>"
    if isinstance(description_field, dict):
        return adf_node_to_html(description_field, jira_creds=jira_creds)
    return ""


def adf_to_plain_text(description_field) -> str:
    if not description_field:
        return ""
    if isinstance(description_field, str):
        return description_field
    if isinstance(description_field, dict):
        parts = []
        _extract_text(description_field, parts)
        return " ".join(parts)
    return ""


def _extract_text(node: dict, parts: list):
    if not isinstance(node, dict):
        return
    if node.get("type") == "text":
        t = node.get("text", "").strip()
        if t:
            parts.append(t)
    if node.get("type") == "hardBreak":
        parts.append("\n")
    for child in node.get("content", []):
        _extract_text(child, parts)


# ============================================
# JIRA ENDPOINTS
# ============================================

@app.get("/jira/details/{issue_key}")
def get_issue_details(issue_key: str):
    jira_base_url = os.getenv("JIRA_BASE_URL")
    jira_email    = os.getenv("JIRA_EMAIL")
    jira_token    = os.getenv("JIRA_API_TOKEN")
    if not all([jira_base_url, jira_email, jira_token]):
        return {"error": "Missing Jira credentials in environment variables."}
    jira_creds = {"base_url": jira_base_url, "email": jira_email, "token": jira_token}
    url     = f"{jira_base_url}/rest/api/3/issue/{issue_key}"
    headers = {"Accept": "application/json"}
    auth    = (jira_email, jira_token)
    try:
        response = requests.get(url, headers=headers, auth=auth)
        response.raise_for_status()
        data   = response.json()
        fields = data.get("fields", {})
        print('fields are', fields)

        start_date = (
            fields.get("customfield_10015")
            or fields.get("startdate")
            or fields.get("created")
        )

        end_date = get_qa_date_from_changelog(
            issue_key, jira_base_url, jira_email, jira_token
        )

        details = {
            "key":             data.get("key"),
            "summary":         fields.get("summary"),
            "description":     adf_to_plain_text(fields.get("description")),
            "descriptionHtml": adf_to_html(fields.get("description"), jira_creds={**jira_creds, "issue_key": data.get("key")}),
            "status":          fields.get("status", {}).get("name"),
            "assignee":        fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
            "reporter":        fields.get("reporter", {}).get("displayName") if fields.get("reporter") else None,
            "priority":        fields.get("priority", {}).get("name") if fields.get("priority") else None,
            "priorityId":      fields.get("priority", {}).get("id") if fields.get("priority") else None,
            "project":         fields.get("project", {}).get("name") if fields.get("project") else None,
            "startDate":       start_date,
            "endDate":         end_date,
            "dueDate":         fields.get("duedate"),
            "labels":          fields.get("labels", []),
            "timeSpent":       fields.get("timespent"),
            "timeEstimate":    fields.get("timeestimate"),
        }
        return {"details": details}
    except Exception as e:
        return {"error": str(e)}


@app.get("/jira/issues/{issue_key}/worklogs")
def get_issue_worklogs(issue_key: str):
    jira_base_url = os.getenv("JIRA_BASE_URL")
    jira_email    = os.getenv("JIRA_EMAIL")
    jira_token    = os.getenv("JIRA_API_TOKEN")
    if not all([jira_base_url, jira_email, jira_token]):
        return {"error": "Missing Jira credentials in environment variables."}
    url     = f"{jira_base_url}/rest/api/3/issue/{issue_key}/worklog"
    headers = {"Accept": "application/json"}
    auth    = (jira_email, jira_token)
    try:
        response = requests.get(url, headers=headers, auth=auth)
        response.raise_for_status()
        data = response.json()
        return {"worklogs": [
            {"author": l.get("author", {}).get("displayName"), "timeSpent": l.get("timeSpent"),
             "started": l.get("started"), "comment": l.get("comment")}
            for l in data.get("worklogs", [])
        ]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/jira/users")
def get_jira_users():
    jira_base_url = os.getenv("JIRA_BASE_URL")
    jira_email    = os.getenv("JIRA_EMAIL")
    jira_token    = os.getenv("JIRA_API_TOKEN")
    if not all([jira_base_url, jira_email, jira_token]):
        return {"error": "Missing Jira credentials in environment variables."}
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    url     = f"{jira_base_url}/rest/api/3/users/search"
    try:
        response = requests.get(url, headers=headers, params={"maxResults": 1000}, auth=auth)
        response.raise_for_status()
        users_list = [
            {"username": u.get("name"), "accountId": u.get("accountId"),
             "displayName": u.get("displayName"), "emailAddress": u.get("emailAddress"),
             "active": u.get("active")}
            for u in response.json()
        ]
        return {"total_users": len(users_list), "users": users_list}
    except Exception as e:
        return {"error": f"Failed to fetch users: {str(e)}"}


@app.get("/jira/issues")
def get_jira_issues(include_full_details: bool = True):
    jira_base_url    = os.getenv("JIRA_BASE_URL")
    jira_email       = os.getenv("JIRA_EMAIL")
    jira_token       = os.getenv("JIRA_API_TOKEN")
    jira_project_key = os.getenv("JIRA_PROJECT_KEY")
    if not all([jira_base_url, jira_email, jira_token, jira_project_key]):
        return {"error": "Missing Jira credentials in environment variables."}
    jira_creds = {"base_url": jira_base_url, "email": jira_email, "token": jira_token}
    auth    = (jira_email, jira_token)
    try:
        raw_issues = fetch_all_jira_issues(
            jira_base_url, jira_email, jira_token,
            jql=f"project = {jira_project_key} ORDER BY created DESC",
            fields=["summary", "status", "assignee", "priority", "created", "updated",
                    "duedate", "issuetype", "labels", "description", "comment", "worklog",
                    "timeestimate", "timespent", "reporter", "project", "parent"]
        )
        issues = []
        for issue in raw_issues:
            if include_full_details:
                issue_key = issue.get("key")
                try:
                    det = requests.get(f"{jira_base_url}/rest/api/3/issue/{issue_key}",
                                       headers={"Accept": "application/json"}, auth=auth)
                    det.raise_for_status()
                    d = det.json()
                    f = d.get("fields", {})

                    start_date = (
                        f.get("customfield_10015")
                        or f.get("startdate")
                        or f.get("created")
                    )

                    end_date = get_qa_date_from_changelog(
                        issue_key, jira_base_url, jira_email, jira_token
                    )

                    issues.append({
                        "key": d.get("key"), "id": d.get("id"), "summary": f.get("summary"),
                        "description":     adf_to_plain_text(f.get("description")),
                        "descriptionHtml": adf_to_html(f.get("description"), jira_creds={**jira_creds, "issue_key": d.get("key")}),
                        "status":        f.get("status", {}).get("name"),
                        "assignee":      f.get("assignee", {}).get("displayName") if f.get("assignee") else None,
                        "assigneeEmail": f.get("assignee", {}).get("emailAddress") if f.get("assignee") else None,
                        "priority":      f.get("priority", {}).get("name") if f.get("priority") else None,
                        "issueType":     f.get("issuetype", {}).get("name") if f.get("issuetype") else None,
                        "project":       f.get("project", {}).get("name") if f.get("project") else None,
                        "created": f.get("created"), "updated": f.get("updated"),
                        "startDate": start_date,
                        "endDate":   end_date,
                        "dueDate": f.get("duedate"), "labels": f.get("labels", []),
                        "worklogs": [{"author": w.get("author", {}).get("displayName"),
                                      "timeSpent": w.get("timeSpent"), "started": w.get("started")}
                                     for w in f.get("worklog", {}).get("worklogs", [])],
                    })
                except Exception:
                    continue
            else:
                issues.append({"id": issue.get("id"), "key": issue.get("key"),
                                "summary": issue.get("fields", {}).get("summary")})
        return {"total_issues": len(issues), "issues": issues}
    except Exception as e:
        return {"error": str(e)}


@app.get("/jira/eod-tickets")
def get_eod_tickets():
    jira_base_url    = os.getenv("JIRA_BASE_URL")
    jira_email       = os.getenv("JIRA_EMAIL")
    jira_token       = os.getenv("JIRA_API_TOKEN")
    jira_project_key = os.getenv("JIRA_PROJECT_KEY")
    if not all([jira_base_url, jira_email, jira_token, jira_project_key]):
        return {"error": "Missing Jira credentials in environment variables."}
    today   = datetime.now().date()
    headers = {"Accept": "application/json"}
    auth    = (jira_email, jira_token)
    try:
        picker_resp = requests.get(f"{jira_base_url}/rest/api/3/issue/picker",
                                   headers=headers, params={"query": jira_project_key}, auth=auth)
        picker_resp.raise_for_status()
        issue_keys = [issue.get("key") for section in picker_resp.json().get("sections", [])
                      for issue in section.get("issues", []) if issue.get("key")]
    except Exception as e:
        return {"error": f"Failed to fetch issues: {str(e)}"}
    tickets_today = []
    for issue_key in issue_keys:
        try:
            wl_resp = requests.get(f"{jira_base_url}/rest/api/3/issue/{issue_key}/worklog",
                                   headers=headers, auth=auth)
            wl_resp.raise_for_status()
            for wl in wl_resp.json().get("worklogs", []):
                if wl.get("author", {}).get("emailAddress") == jira_email and wl.get("started"):
                    try:
                        wl_date = datetime.strptime(wl["started"][:10], "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if wl_date == today:
                        try:
                            det = requests.get(f"{jira_base_url}/rest/api/3/issue/{issue_key}",
                                               headers=headers, auth=auth)
                            det.raise_for_status()
                            d = det.json()
                            f = d.get("fields", {})
                            tickets_today.append({"key": d.get("key"), "summary": f.get("summary"),
                                                  "status": f.get("status", {}).get("name")})
                        except Exception:
                            pass
                        break
        except Exception:
            continue
    return {"tickets": tickets_today}


# ============================================
# AZURE DEVOPS HELPERS
# ============================================

def get_azure_auth_header():
    azure_pat = os.getenv("AZURE_DEVOPS_PAT")
    if not azure_pat:
        return None
    return f"Basic {base64.b64encode(f':{azure_pat}'.encode()).decode()}"


def fetch_azure_work_items_by_ids(azure_org, azure_project, azure_headers, work_item_ids):
    all_items = []
    for i in range(0, len(work_item_ids), 200):
        batch   = work_item_ids[i:i + 200]
        ids_str = ",".join(str(wid) for wid in batch)
        url     = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems?ids={ids_str}&api-version=7.1"
        response = requests.get(url, headers=azure_headers)
        response.raise_for_status()
        all_items.extend(response.json().get("value", []))
    return all_items


def map_hours_to_story_points(hours: float) -> int:
    FIBONACCI_POINTS = [1, 2, 3, 5, 8, 13, 21]
    if not hours or hours <= 0:
        result = 1
    else:
        result = min(FIBONACCI_POINTS, key=lambda sp: abs(sp - hours))
    print(f"    [STORY POINTS] {hours}h → {result} story points")
    return result

def map_jira_priority_to_azure(jira_priority: str) -> int:
    name_map = {
        "highest": 1,
        "high":    2,
        "medium":  3,
        "low":     4,
    }
    normalised     = (jira_priority or "").strip().lower()
    numeric_value  = name_map.get(normalised, 3)
    print(f"    [PRIORITY MAP] Jira '{jira_priority}' → Microsoft.VSTS.Common.Priority={numeric_value}")
    return numeric_value

def map_jira_priority_to_azure_custom(jira_priority: str) -> str:
    name_map = {
        "highest": "1 - Very High",
        "high":    "2 - High",
        "medium":  "3 - Medium",
        "low":     "4 - Low",
    }
    normalised    = (jira_priority or "").strip().lower()
    custom_value  = name_map.get(normalised, "3 - Medium")
    print(f"    [PRIORITY MAP] Jira '{jira_priority}' → Custom.PriorityI='{custom_value}'")
    return custom_value


def get_assignee_email_overrides() -> dict:
    """
    Some people's Jira login email differs from their Azure DevOps/AD email
    (e.g. a personal address used for Jira vs. a corporate alias for Azure DevOps).
    Configure overrides as JSON via ASSIGNEE_EMAIL_OVERRIDES, e.g.
    {"jira.address@example.com": "azure.address@example.com"}.
    """
    raw = os.getenv("ASSIGNEE_EMAIL_OVERRIDES", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"    [EMAIL OVERRIDES] Could not parse ASSIGNEE_EMAIL_OVERRIDES: {e}")
        return {}


def resolve_assignee_email(jira_email: str) -> str:
    if not jira_email:
        return jira_email
    mapped = get_assignee_email_overrides().get(jira_email, jira_email)
    if mapped != jira_email:
        print(f"    [EMAIL OVERRIDE] '{jira_email}' → '{mapped}'")
    return mapped


def resolve_target_date(jira_issue) -> str | None:
    """
    Azure's DueDate/TargetDate should reflect the first QA transition in Jira.
    If the ticket hasn't reached QA yet, fall back to Jira's Due Date so Azure
    processes that require a Target Date (once a Story leaves Backlog) don't
    reject the create/update. Only truly dateless tickets are left unset.
    """
    qa_date = jira_issue.get("endDate")
    if qa_date:
        return qa_date
    due_date = jira_issue.get("dueDate")
    if due_date:
        print(f"    [TARGET DATE] No QA date — falling back to Jira Due Date '{due_date}'")
        return due_date
    return None


def build_azure_patch_document(jira_issue, area_path, available_story_states: list = None, is_update: bool = False):
    title          = jira_issue.get("summary", "Untitled")
    jira_status    = jira_issue.get("status", "To Do")
    azure_state    = resolve_story_state(jira_status, available_story_states or [], assignee_email=jira_issue.get("assigneeEmail"))
    azure_priority = map_jira_priority_to_azure(jira_issue.get("priority", "Medium"))
    azure_priority_custom = map_jira_priority_to_azure_custom(jira_issue.get("priority", "Medium"))


    description_html = jira_issue.get("descriptionHtml") or ""
    if not description_html:
        plain = jira_issue.get("description") or ""
        description_html = f"<p>{plain}</p>" if plain else "<p>(No description provided in Jira.)</p>"

    op = "replace" if is_update else "add"
    print(f"    [PATCH DOC] Building Story patch. is_update={is_update} → op='{op}'")
    print(f"    [PATCH DOC] priority_value={azure_priority} state='{azure_state}' title='{title}'")
    time_spent_secs = jira_issue.get("timeSpent") or 0
    original_hours = round(time_spent_secs / 3600, 2) if time_spent_secs else 0.0
    story_points = map_hours_to_story_points(original_hours)


    patch_document = [
        {"op": op, "path": "/fields/System.Title",                               "value": title},
        {"op": op, "path": "/fields/System.Description",                         "value": description_html},
        {"op": op, "path": "/fields/System.State",                               "value": azure_state},
        {"op": op, "path": "/fields/Microsoft.VSTS.Common.Priority",             "value": azure_priority},
        {"op": op, "path": "/fields/Custom.PriorityI",                           "value": azure_priority_custom},
        {"op": op, "path": "/fields/System.AreaPath",                            "value": area_path},
        {"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StoryPoints", "value": story_points},
        {"op": op, "path": "/fields/Custom.StoryPointsI", "value": str(story_points)},
        {"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate", "value": original_hours},
    ]

    start_date = jira_issue.get("startDate")
    if start_date:
        patch_document.append({
            "op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StartDate", "value": start_date
        })
        print(f"    [PATCH DOC] StartDate → {start_date}")

    end_date = resolve_target_date(jira_issue)
    if end_date:
        patch_document.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.DueDate",    "value": end_date})
        patch_document.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate", "value": end_date})
        print(f"    [PATCH DOC] DueDate/TargetDate → {end_date}")
    else:
        print(f"    [PATCH DOC] No QA date or Jira due date — DueDate/TargetDate NOT set")

    assignee_email = resolve_assignee_email(jira_issue.get("assigneeEmail"))
    if assignee_email:
        patch_document.append({"op": op, "path": "/fields/System.AssignedTo", "value": assignee_email})
        print(f"    [PATCH DOC] AssignedTo → {assignee_email}")

    print(f"    [PATCH DOC] Final patch has {len(patch_document)} ops, all op='{op}'")
    return patch_document


def build_azure_task_patch_document(jira_issue, area_path, available_task_states: list = None, is_update: bool = False):
    title          = jira_issue.get("summary", "Untitled")
    jira_status    = jira_issue.get("status", "To Do")
    azure_state    = resolve_task_state(jira_status, available_task_states or [], assignee_email=jira_issue.get("assigneeEmail"))
    azure_priority = map_jira_priority_to_azure(jira_issue.get("priority", "Medium"))

    description_html = jira_issue.get("descriptionHtml") or ""
    if not description_html:
        plain = jira_issue.get("description") or ""
        description_html = f"<p>{plain}</p>" if plain else "<p>(No description provided in Jira.)</p>"

    time_spent_secs      = jira_issue.get("timeSpent") or 0
    original_hours       = round(time_spent_secs / 3600, 2) if time_spent_secs else 1.0

    op = "replace" if is_update else "add"
    print(f"    [PATCH TASK DOC] Building Task patch. is_update={is_update} → op='{op}'")
    print(f"    [PATCH TASK DOC] priority_value={azure_priority} state='{azure_state}' title='{title}'")

    azure_priority        = map_jira_priority_to_azure(jira_issue.get("priority", "Medium"))
    azure_priority_custom = map_jira_priority_to_azure_custom(jira_issue.get("priority", "Medium"))

    patch_document = [
        {"op": op, "path": "/fields/System.Title",                                    "value": title},
        {"op": op, "path": "/fields/System.Description",                              "value": description_html},
        {"op": op, "path": "/fields/System.State",                                    "value": azure_state},
        {"op": op, "path": "/fields/Microsoft.VSTS.Common.Priority",                  "value": azure_priority},
        {"op": op, "path": "/fields/Custom.PriorityI",                                "value": azure_priority_custom},
        {"op": op, "path": "/fields/System.AreaPath",                                 "value": area_path},
        {"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate", "value": original_hours},
        {"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.RemainingWork",    "value": original_hours},
    ]

    start_date = jira_issue.get("startDate")
    if start_date:
        patch_document.append({
            "op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StartDate", "value": start_date
        })
        print(f"    [PATCH TASK DOC] StartDate → {start_date}")

    end_date = resolve_target_date(jira_issue)
    if end_date:
        patch_document.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.DueDate",    "value": end_date})
        patch_document.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate", "value": end_date})
        print(f"    [PATCH TASK DOC] DueDate/TargetDate → {end_date}")
    else:
        print(f"    [PATCH TASK DOC] No QA date or Jira due date — DueDate/TargetDate NOT set")

    assignee_email = resolve_assignee_email(jira_issue.get("assigneeEmail"))
    if assignee_email:
        patch_document.append({"op": op, "path": "/fields/System.AssignedTo", "value": assignee_email})
        print(f"    [PATCH TASK DOC] AssignedTo → {assignee_email}")

    print(f"    [PATCH TASK DOC] Final patch has {len(patch_document)} ops, all op='{op}'")
    return patch_document


def create_child_task_under_story(azure_org, azure_project, azure_team, azure_headers, story_id, jira_issue, available_task_states=None):
    area_path  = f"{azure_project}\\{azure_team}"
    task_patch = build_azure_task_patch_document(jira_issue, area_path, available_task_states, is_update=False)
    task_url   = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/$Task?api-version=7.1"
    headers    = {**azure_headers, "Content-Type": "application/json-patch+json"}

    print(f"\n      [TASK CREATE] Creating child Task under Story ID={story_id}")
    print(f"      [TASK CREATE] URL: {task_url}")
    print(f"      [TASK CREATE] Patch document:\n{json.dumps(task_patch, indent=8)}")

    task_response = requests.post(task_url, headers=headers, json=task_patch)
    print(f"      [TASK CREATE] Response status: {task_response.status_code}")
    print(f"      [TASK CREATE] Response body: {task_response.text}")
    task_response.raise_for_status()

    task_data    = task_response.json()
    task_id      = task_data.get("id")
    task_api_url = task_data.get("url")
    print(f"      [TASK CREATE] Task created. ID={task_id} URL={task_api_url}")

    link_patch = [{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{story_id}",
            "attributes": {"comment": f"Child task of User Story {story_id} (synced from JIRA {jira_issue.get('key', '')})"}
        }
    }]
    link_url = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{task_id}?api-version=7.1"

    print(f"\n      [TASK LINK] Linking Task {task_id} as child of Story {story_id}")
    print(f"      [TASK LINK] PATCH URL: {link_url}")
    print(f"      [TASK LINK] Link patch:\n{json.dumps(link_patch, indent=8)}")

    link_response = requests.patch(link_url, headers=headers, json=link_patch)
    print(f"      [TASK LINK] Response status: {link_response.status_code}")
    print(f"      [TASK LINK] Response body: {link_response.text}")
    link_response.raise_for_status()
    print(f"      [TASK LINK] ✓ Task {task_id} linked as child of Story {story_id}")

    return {
        "task_id":           task_id,
        "task_url":          task_api_url,
        "title":             task_data.get("fields", {}).get("System.Title"),
        "state":             task_data.get("fields", {}).get("System.State"),
        "linked_to_story_id": story_id,
    }


def get_child_task_ids_for_story(azure_org, azure_project, azure_headers, story_id):
    url = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=7.1"
    print(f"\n      [CHILD CHECK] Fetching relations for Story {story_id}")
    response = requests.get(url, headers=azure_headers)
    print(f"      [CHILD CHECK] Response status: {response.status_code}")
    response.raise_for_status()

    relations = response.json().get("relations", [])
    print(f"      [CHILD CHECK] Total relations found: {len(relations)}")
    for rel in relations:
        print(f"        rel type={rel.get('rel')} url={rel.get('url')}")

    child_ids = []
    for rel in relations:
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
            rel_url = rel.get("url", "")
            try:
                child_id = int(rel_url.rstrip("/").split("/")[-1])
                child_ids.append(child_id)
                print(f"      [CHILD CHECK] Found child work item ID: {child_id}")
            except Exception as e:
                print(f"      [CHILD CHECK] Could not parse child ID from URL {rel_url}: {e}")

    print(f"      [CHILD CHECK] Total child Task IDs: {child_ids}")
    return child_ids


def create_azure_work_item(azure_org, azure_project, azure_team, azure_headers, jira_issue, state_cache=None):
    area_path      = f"{azure_project}\\{azure_team}"
    story_states   = (state_cache or {}).get("User Story", [])
    task_states    = (state_cache or {}).get("Task", [])
    patch_document = build_azure_patch_document(jira_issue, area_path, story_states, is_update=False)

    url     = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/$User%20Story?api-version=7.1"
    headers = {**azure_headers, "Content-Type": "application/json-patch+json"}

    print(f"\n    [CREATE STORY] URL: {url}")
    print(f"    [CREATE STORY] Area path: {area_path}")
    print(f"    [CREATE STORY] Patch document:\n{json.dumps(patch_document, indent=6)}")

    response = requests.post(url, headers=headers, json=patch_document)
    print(f"    [CREATE STORY] Response status: {response.status_code}")
    print(f"    [CREATE STORY] Response body: {response.text}")
    response.raise_for_status()

    created  = response.json()
    story_id = created.get("id")
    print(f"    [CREATE STORY] ✓ Story created. ID={story_id}")

    task_result = None
    try:
        task_result = create_child_task_under_story(
            azure_org, azure_project, azure_team, azure_headers, story_id, jira_issue, task_states
        )
        print(f"    [CREATE STORY] ✓ Child Task created: {task_result}")
    except Exception as e:
        print(f"    [CREATE STORY] ✗ Child Task creation FAILED: {str(e)}")
        task_result = {"error": str(e)}

    return {
        "azure_id":   story_id,
        "azure_url":  created.get("url"),
        "title":      created.get("fields", {}).get("System.Title"),
        "state":      created.get("fields", {}).get("System.State"),
        "child_task": task_result,
    }


def update_azure_work_item(azure_org, azure_project, azure_team, azure_headers, azure_id, jira_issue, state_cache=None, existing_work_item_type=None):
    area_path = f"{azure_project}\\{azure_team}"
    sc = state_cache or {}

    if not existing_work_item_type:
        try:
            fetch_resp = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{azure_id}?api-version=7.1",
                headers=azure_headers
            )
            if fetch_resp.status_code == 200:
                existing_work_item_type = fetch_resp.json().get("fields", {}).get("System.WorkItemType", "User Story")
                print(f"    [UPDATE STORY] Existing item type: '{existing_work_item_type}'")
            else:
                existing_work_item_type = "User Story"
                print(f"    [UPDATE STORY] Could not fetch item type ({fetch_resp.status_code}), assuming 'User Story'")
        except Exception as e:
            existing_work_item_type = "User Story"
            print(f"    [UPDATE STORY] ERROR fetching item type: {e}, assuming 'User Story'")

    story_states = sc.get(existing_work_item_type) or sc.get("User Story", [])
    task_states  = sc.get("Task", [])
    print(f"    [UPDATE STORY] Using states for type '{existing_work_item_type}': {story_states}")

    azure_priority        = map_jira_priority_to_azure(jira_issue.get("priority", "Medium"))
    azure_priority_custom = map_jira_priority_to_azure_custom(jira_issue.get("priority", "Medium"))
    print(f"    [UPDATE STORY] JIRA priority='{jira_issue.get('priority')}' → "
          f"azure_priority={azure_priority} custom='{azure_priority_custom}'")
    print(f"    [UPDATE STORY] JIRA priority='{jira_issue.get('priority')}' → azure_priority={azure_priority}")

    patch_document = build_azure_patch_document(jira_issue, area_path, story_states, is_update=True)
    url     = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{azure_id}?api-version=7.1"
    headers = {**azure_headers, "Content-Type": "application/json-patch+json"}

    print(f"\n    [UPDATE STORY] URL: {url}")
    print(f"    [UPDATE STORY] Area path: {area_path}")
    print(f"    [UPDATE STORY] Patch document:\n{json.dumps(patch_document, indent=6)}")

    response = requests.patch(url, headers=headers, json=patch_document)
    print(f"    [UPDATE STORY] Response status: {response.status_code}")
    print(f"    [UPDATE STORY] Response body: {response.text}")
    response.raise_for_status()

    updated = response.json()

    print(f"\n    [UPDATE STORY] ══ READ-BACK VERIFICATION ══")
    print(f"    [UPDATE STORY] Priority we SENT in patch : {azure_priority}  (from jira: '{jira_issue.get('priority')}')")
    print(f"    [UPDATE STORY] Priority in PATCH RESPONSE: {updated.get('fields', {}).get('Microsoft.VSTS.Common.Priority')}")

    verify_resp = requests.get(
        f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{azure_id}?api-version=7.1",
        headers=azure_headers
    )
    if verify_resp.status_code == 200:
        stored = verify_resp.json().get("fields", {})
        print(f"    [UPDATE STORY] Priority in FRESH GET    : {stored.get('Microsoft.VSTS.Common.Priority')}")
        print(f"    [UPDATE STORY] State   in FRESH GET     : {stored.get('System.State')}")
        print(f"    [UPDATE STORY] Title   in FRESH GET     : {stored.get('System.Title')}")
        if stored.get('Microsoft.VSTS.Common.Priority') != azure_priority:
            print(f"    [UPDATE STORY] ⚠️  MISMATCH! Sent={azure_priority} but stored={stored.get('Microsoft.VSTS.Common.Priority')}")
        else:
            print(f"    [UPDATE STORY] ✓ Priority confirmed correctly stored as {azure_priority}")
    else:
        print(f"    [UPDATE STORY] ✗ Read-back GET failed: {verify_resp.status_code}")
    print(f"    [UPDATE STORY] ══════════════════════════════")
    print(f"    [UPDATE STORY] ✓ Story updated. ID={azure_id}")

    task_result = None
    try:
        child_ids = get_child_task_ids_for_story(azure_org, azure_project, azure_headers, azure_id)
        if child_ids:
            print(f"    [UPDATE STORY] Found existing child Task(s): {child_ids} — updating them to stay in sync...")
            updated_tasks = []
            for child_id in child_ids:
                try:
                    child_patch = build_azure_task_patch_document(jira_issue, area_path, task_states, is_update=True)
                    child_url   = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{child_id}?api-version=7.1"
                    child_hdrs  = {**azure_headers, "Content-Type": "application/json-patch+json"}
                    print(f"    [UPDATE TASK] Patching existing child Task {child_id}...")
                    child_resp = requests.patch(child_url, headers=child_hdrs, json=child_patch)
                    print(f"    [UPDATE TASK] Response status: {child_resp.status_code}")
                    child_resp.raise_for_status()

                    child_verify = requests.get(
                        f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{child_id}?api-version=7.1",
                        headers=azure_headers
                    )
                    if child_verify.status_code == 200:
                        child_stored          = child_verify.json().get("fields", {})
                        child_actual_priority = child_stored.get("Microsoft.VSTS.Common.Priority")
                        child_actual_state    = child_stored.get("System.State")
                        print(f"    [UPDATE TASK] ══ READ-BACK for Task {child_id} ══")
                        print(f"    [UPDATE TASK] Priority SENT     : {azure_priority}")
                        print(f"    [UPDATE TASK] Priority STORED   : {child_actual_priority}")
                        print(f"    [UPDATE TASK] State   STORED    : {child_actual_state}")
                        if child_actual_priority != azure_priority:
                            print(f"    [UPDATE TASK] ⚠️  MISMATCH! Sent={azure_priority} but Task stored={child_actual_priority}")
                        else:
                            print(f"    [UPDATE TASK] ✓ Task priority confirmed as {child_actual_priority}")
                        print(f"    [UPDATE TASK] ══════════════════════════════════════")

                    print(f"    [UPDATE TASK] ✓ Child Task {child_id} updated")
                    updated_tasks.append({"task_id": child_id, "action": "updated", "status": "success"})
                except Exception as e:
                    print(f"    [UPDATE TASK] ✗ Failed to update child Task {child_id}: {e}")
                    updated_tasks.append({"task_id": child_id, "action": "updated", "status": "error", "error": str(e)})
            task_result = {"updated_child_tasks": updated_tasks}
        else:
            print(f"    [UPDATE STORY] No child Tasks found — creating one...")
            task_result = create_child_task_under_story(
                azure_org, azure_project, azure_team, azure_headers, azure_id, jira_issue, task_states
            )
            print(f"    [UPDATE STORY] ✓ Child Task created: {task_result}")
    except Exception as e:
        print(f"    [UPDATE STORY] ✗ Child Task check/creation FAILED: {str(e)}")
        task_result = {"error": str(e)}

    return {
        "azure_id":   updated.get("id"),
        "azure_url":  updated.get("url"),
        "title":      updated.get("fields", {}).get("System.Title"),
        "state":      updated.get("fields", {}).get("System.State"),
        "child_task": task_result,
    }



def fetch_all_jira_issues(jira_base_url, jira_email, jira_token, jql, fields):
    """
    The Jira Cloud /rest/api/3/search/jql endpoint paginates via nextPageToken
    rather than startAt, and a single call only returns up to maxResults issues.
    Keep requesting pages until isLast (or no nextPageToken) so projects with
    more than one page of tickets are synced in full.
    """
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    url     = f"{jira_base_url}/rest/api/3/search/jql"

    all_issues  = []
    next_token  = None
    page_number = 0

    while True:
        payload = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_token:
            payload["nextPageToken"] = next_token

        response = requests.post(url, headers=headers, json=payload, auth=auth)
        response.raise_for_status()
        data = response.json()

        page_issues = data.get("issues", [])
        all_issues.extend(page_issues)
        page_number += 1
        print(f"  [JIRA SEARCH] Page {page_number}: fetched {len(page_issues)} issues "
              f"(total so far: {len(all_issues)})")

        next_token = data.get("nextPageToken")
        if data.get("isLast", not next_token) or not next_token or not page_issues:
            break

    return all_issues


def _build_date_jql_clause(date_filter) -> str:
    """Return a JQL clause (no leading AND) for the given DateFilter, or '' if no filter."""
    if not date_filter:
        return ""
    field  = (date_filter.field or "updated").strip().lower()
    from_d = (date_filter.from_date or "").strip()
    to_d   = (date_filter.to_date   or "").strip()
    if not from_d and not to_d:
        return ""

    if field == "either":
        # Ticket whose updated date OR created date falls in range
        if from_d and to_d:
            return (f'(updated >= "{from_d}" AND updated <= "{to_d}")'
                    f' OR (created >= "{from_d}" AND created <= "{to_d}")')
        elif from_d:
            return f'(updated >= "{from_d}" OR created >= "{from_d}")'
        else:
            return f'(updated <= "{to_d}" OR created <= "{to_d}")'
    else:
        # "updated" or "created"
        parts = []
        if from_d:
            parts.append(f'{field} >= "{from_d}"')
        if to_d:
            parts.append(f'{field} <= "{to_d}"')
        return " AND ".join(parts)


def get_jira_issues_for_sync(jira_base_url, jira_email, jira_token, jira_project_key,
                              date_filter=None):
    jira_creds = {"base_url": jira_base_url, "email": jira_email, "token": jira_token}

    date_clause = _build_date_jql_clause(date_filter)
    jql = f"project = {jira_project_key}"
    if date_clause:
        jql += f" AND ({date_clause})"
    jql += " ORDER BY created DESC"
    print(f"  [JIRA SYNC] JQL: {jql}")

    raw_issues = fetch_all_jira_issues(
        jira_base_url, jira_email, jira_token,
        jql=jql,
        fields=["summary", "status", "assignee", "priority", "created", "updated",
                "duedate", "startdate", "issuetype", "labels", "description", "comment",
                "worklog", "timeestimate", "timespent", "reporter", "project",
                "sprint", "fixVersions", "components", "environment", "customfield_10015",
                "*all"]
    )

    issues = []
    for issue in raw_issues:
        fields          = issue.get("fields", {})
        raw_description = fields.get("description")
        issue_key       = issue.get("key")

        print(f"\n  {'='*60}")
        print(f"  [JIRA RAW] Full data for {issue_key}:")
        print(f"  [JIRA RAW] key={issue_key}  id={issue.get('id')}  self={issue.get('self')}")

        for fkey, fval in sorted(fields.items()):
            if fkey == "description":
                print(f"  [JIRA FIELD] description = <ADF object, {len(str(fval))} chars>")
            elif fkey == "comment":
                comments = (fval or {}).get("comments", [])
                print(f"  [JIRA FIELD] comment = {len(comments)} comment(s)")
            elif fkey == "worklog":
                logs = (fval or {}).get("worklogs", [])
                print(f"  [JIRA FIELD] worklog = {len(logs)} worklog(s)")
            else:
                print(f"  [JIRA FIELD] {fkey} = {json.dumps(fval, default=str)[:300]}")

        start_date = (
            fields.get("customfield_10015")
            or fields.get("startdate")
            or fields.get("created")
        )

        end_date = get_qa_date_from_changelog(
            issue_key, jira_base_url, jira_email, jira_token
        )

        print(f"  [JIRA DATE] customfield_10015={fields.get('customfield_10015')}  "
              f"startdate={fields.get('startdate')}  "
              f"created={fields.get('created')}  → resolved startDate='{start_date}'")
        print(f"  [JIRA DATE] qa_transition(changelog)={end_date}  "
              f"duedate={fields.get('duedate')} (duedate NOT used for endDate)")
        print(f"  {'='*60}")

        issues.append({
            "key":             issue_key,
            "id":              issue.get("id"),
            "summary":         fields.get("summary"),
            "description":     adf_to_plain_text(raw_description),
            "descriptionHtml": adf_to_html(raw_description, jira_creds={**jira_creds, "issue_key": issue_key}),
            "status":          fields.get("status", {}).get("name"),
            "priority":        fields.get("priority", {}).get("name") if fields.get("priority") else None,
            "priorityId":      fields.get("priority", {}).get("id") if fields.get("priority") else None,
            "assignee":        fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
            "assigneeEmail":   fields.get("assignee", {}).get("emailAddress") if fields.get("assignee") else None,
            "reporter":        fields.get("reporter", {}).get("displayName") if fields.get("reporter") else None,
            "issueType":       fields.get("issuetype", {}).get("name") if fields.get("issuetype") else None,
            "project":         fields.get("project", {}).get("name") if fields.get("project") else None,
            "created":         fields.get("created"),
            "updated":         fields.get("updated"),
            "startDate":       start_date,
            "endDate":         end_date,
            "dueDate":         fields.get("duedate"),
            "labels":          fields.get("labels", []),
            "fixVersions":     [v.get("name") for v in fields.get("fixVersions", [])],
            "components":      [c.get("name") for c in fields.get("components", [])],
            "timeSpent":       fields.get("timespent"),
            "timeEstimate":    fields.get("timeestimate"),
            "_rawFields":      {k: v for k, v in fields.items()
                                if k not in ("description", "comment", "worklog")},
            "worklogs": [
                {"author": w.get("author", {}).get("displayName"), "timeSpent": w.get("timeSpent"),
                 "started": w.get("started"), "comment": w.get("comment")}
                for w in fields.get("worklog", {}).get("worklogs", [])
            ],
            "comments": [
                {"author": c.get("author", {}).get("displayName"), "body": c.get("body"), "created": c.get("created")}
                for c in fields.get("comment", {}).get("comments", [])
            ],
        })
    return issues


def get_azure_items_by_title(azure_org, azure_project, azure_team, azure_headers):
    wiql_url   = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/wiql?api-version=7.1"
    wiql_query = {
        "query": f"""SELECT [System.Id], [System.Title], [System.State], [System.ChangedDate]
                     FROM WorkItems
                     WHERE [System.TeamProject] = '{azure_project}'
                     AND [System.AreaPath] UNDER '{azure_project}\\{azure_team}'
                     ORDER BY [System.ChangedDate] DESC"""
    }
    wiql_response = requests.post(wiql_url, headers=azure_headers, json=wiql_query)
    wiql_response.raise_for_status()
    meta = wiql_response.json().get("workItems", [])
    print(f"  Found {len(meta)} Azure work items")

    items_by_id = {
        item.get("id"): {"id": item.get("id"), "title": None, "state": None, "changedDate": None, "workItemType": None}
        for item in meta
    }

    if meta:
        all_ids      = [item.get("id") for item in meta]
        detail_items = fetch_azure_work_items_by_ids(azure_org, azure_project, azure_headers, all_ids)
        for item in detail_items:
            item_id = item.get("id")
            if item_id in items_by_id:
                items_by_id[item_id]["title"]        = item.get("fields", {}).get("System.Title")
                items_by_id[item_id]["state"]        = item.get("fields", {}).get("System.State")
                items_by_id[item_id]["changedDate"]  = item.get("fields", {}).get("System.ChangedDate")
                items_by_id[item_id]["workItemType"] = item.get("fields", {}).get("System.WorkItemType")

    by_title = {}
    TYPE_PRIORITY = {"User Story": 0, "Bug": 1, "Feature": 2, "Epic": 3, "Task": 9}
    for v in items_by_id.values():
        t = v.get("title")
        if not t:
            continue
        if t not in by_title:
            by_title[t] = v
        else:
            existing_prio = TYPE_PRIORITY.get(by_title[t].get("workItemType", ""), 5)
            new_prio      = TYPE_PRIORITY.get(v.get("workItemType", ""), 5)
            if new_prio < existing_prio:
                by_title[t] = v
    print(f"  Indexed {len(by_title)} Azure work items by title")
    return by_title


# ============================================
# SYNC ENDPOINTS
# ============================================

@app.get("/sync/jira-to-azure/preview")
def sync_jira_to_azure_preview():
    """DRY RUN — shows what WOULD happen without making any changes."""
    jira_base_url    = os.getenv("JIRA_BASE_URL")
    jira_email       = os.getenv("JIRA_EMAIL")
    jira_token       = os.getenv("JIRA_API_TOKEN")
    jira_project_key = os.getenv("JIRA_PROJECT_KEY")
    azure_org        = os.getenv("AZURE_DEVOPS_ORG")
    azure_project    = os.getenv("AZURE_DEVOPS_PROJECT")
    azure_team       = os.getenv("AZURE_DEVOPS_TEAM")

    if not all([jira_base_url, jira_email, jira_token, jira_project_key]):
        return {"error": "Missing JIRA credentials"}
    if not all([azure_org, azure_project, azure_team]):
        return {"error": "Missing Azure DevOps credentials"}

    print("\n" + "="*80)
    print("SYNC PREVIEW: JIRA → AZURE DEVOPS (DRY RUN)")
    print(f"  JIRA project:  {jira_project_key}")
    print(f"  Azure org:     {azure_org}")
    print(f"  Azure project: {azure_project}")
    print(f"  Azure team:    {azure_team}")
    print("="*80)

    try:
        jira_issues = get_jira_issues_for_sync(jira_base_url, jira_email, jira_token, jira_project_key)
        print(f"[JIRA] Found {len(jira_issues)} tickets")
    except Exception as e:
        return {"error": f"Failed to fetch JIRA tickets: {str(e)}"}

    azure_auth_header = get_azure_auth_header()
    if not azure_auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    azure_headers = {"Content-Type": "application/json", "Authorization": azure_auth_header}
    try:
        azure_by_title = get_azure_items_by_title(azure_org, azure_project, azure_team, azure_headers)
    except Exception as e:
        return {"error": f"Failed to fetch Azure work items: {str(e)}"}

    preview = {"to_create": [], "to_update": [], "to_skip": [], "with_errors": []}

    for jira_issue in jira_issues:
        issue_key    = jira_issue.get("key")
        jira_title   = jira_issue.get("summary")
        jira_updated = jira_issue.get("updated")

        try:
            jira_updated_dt = datetime.fromisoformat(jira_updated.replace("Z", "+00:00"))
            if jira_updated_dt.tzinfo is None:
                jira_updated_dt = jira_updated_dt.replace(tzinfo=timezone.utc)
            else:
                jira_updated_dt = jira_updated_dt.astimezone(timezone.utc)
        except Exception:
            jira_updated_dt = datetime.now(timezone.utc)

        if jira_title in azure_by_title:
            azure_item = azure_by_title[jira_title]
            try:
                azure_changed_dt = datetime.fromisoformat(azure_item["changedDate"].replace("Z", "+00:00"))
                if azure_changed_dt.tzinfo is None:
                    azure_changed_dt = azure_changed_dt.replace(tzinfo=timezone.utc)
                else:
                    azure_changed_dt = azure_changed_dt.astimezone(timezone.utc)
            except Exception:
                azure_changed_dt = datetime.min.replace(tzinfo=timezone.utc)

            if jira_updated_dt > azure_changed_dt:
                preview["to_update"].append({
                    "action": "UPDATE", "jira_key": issue_key, "jira_title": jira_title,
                    "azure_id": azure_item["id"], "azure_state": azure_item["state"],
                    "jira_updated": str(jira_updated_dt), "azure_changedDate": str(azure_changed_dt),
                    "reason": "JIRA is NEWER than Azure",
                    "note": "Will also check if child Task exists; creates one if missing.",
                    "resolved_startDate": jira_issue.get("startDate"),
                    "resolved_endDate":   jira_issue.get("endDate"),
                    "jira_data": jira_issue
                })
            else:
                preview["to_skip"].append({
                    "action": "SKIP", "jira_key": issue_key, "jira_title": jira_title,
                    "azure_id": azure_item["id"], "reason": "Azure is NEWER or EQUAL",
                    "jira_updated": str(jira_updated_dt), "azure_changedDate": str(azure_changed_dt),
                })
        else:
            preview["to_create"].append({
                "action": "CREATE", "jira_key": issue_key, "jira_title": jira_title,
                "reason": "No matching Azure work item found",
                "note": "Will create User Story + child Task (same fields) for time logging.",
                "resolved_startDate": jira_issue.get("startDate"),
                "resolved_endDate":   jira_issue.get("endDate"),
                "jira_data": jira_issue
            })

    return {
        "status": "preview", "is_dry_run": True,
        "message": "DRY RUN — no changes made. Call POST /sync/jira-to-azure to execute.",
        "timestamp": str(datetime.now()),
        "summary": {
            "total_jira_tickets": len(jira_issues), "total_azure_items": len(azure_by_title),
            "to_create_count": len(preview["to_create"]), "to_update_count": len(preview["to_update"]),
            "to_skip_count": len(preview["to_skip"]), "error_count": len(preview["with_errors"])
        },
        "preview": preview
    }


@app.post("/sync/jira-to-azure")
def sync_jira_to_azure():
    jira_base_url    = os.getenv("JIRA_BASE_URL")
    jira_email       = os.getenv("JIRA_EMAIL")
    jira_token       = os.getenv("JIRA_API_TOKEN")
    jira_project_key = os.getenv("JIRA_PROJECT_KEY")
    azure_org        = os.getenv("AZURE_DEVOPS_ORG")
    azure_project    = os.getenv("AZURE_DEVOPS_PROJECT")
    azure_team       = os.getenv("AZURE_DEVOPS_TEAM")

    if not all([jira_base_url, jira_email, jira_token, jira_project_key]):
        return {"error": "Missing JIRA credentials"}
    if not all([azure_org, azure_project, azure_team]):
        return {"error": "Missing Azure DevOps credentials"}

    print("\n" + "="*80)
    print("STARTING LIVE SYNC: JIRA → AZURE DEVOPS")
    print(f"  JIRA project:  {jira_project_key}")
    print(f"  Azure org:     {azure_org}")
    print(f"  Azure project: {azure_project}")
    print(f"  Azure team:    {azure_team}")
    print(f"  Area path:     {azure_project}\\{azure_team}")
    print(f"  StartDate:     customfield_10015 (planned start date from Jira)")
    print(f"  EndDate:       First 'QA/Testing' transition from Jira changelog (null if never QA'd)")
    print(f"  Images:        Downloaded from Jira + embedded as base64 data URIs in HTML")
    print("="*80)

    print("\n[STEP 1] Fetching JIRA tickets (changelog + images)...")
    try:
        jira_issues = get_jira_issues_for_sync(jira_base_url, jira_email, jira_token, jira_project_key)
        print(f"  Found {len(jira_issues)} JIRA tickets")
        for ji in jira_issues:
            print(f"    - {ji['key']}: '{ji['summary']}' | status={ji['status']} | "
                  f"priority={ji['priority']} | startDate={ji.get('startDate')} | "
                  f"endDate={ji.get('endDate')} | timeEstimate={ji.get('timeEstimate')}")
    except Exception as e:
        return {"error": f"Failed to fetch JIRA tickets: {str(e)}"}

    print("\n[STEP 2] Fetching Azure work items + valid states...")
    azure_auth_header = get_azure_auth_header()
    if not azure_auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    azure_headers = {"Content-Type": "application/json", "Authorization": azure_auth_header}

    state_cache = get_or_load_state_cache(azure_org, azure_project, azure_auth_header)
    print(f"  [STATE CACHE] User Story states: {state_cache.get('User Story', [])}")
    print(f"  [STATE CACHE] Task states:        {state_cache.get('Task', [])}")

    try:
        azure_by_title = get_azure_items_by_title(azure_org, azure_project, azure_team, azure_headers)
    except Exception as e:
        return {"error": f"Failed to fetch Azure work items: {str(e)}"}

    print("\n[STEP 3] Syncing tickets...")
    sync_report = {"created": [], "updated": [], "skipped": [], "failed": []}

    for jira_issue in jira_issues:
        issue_key    = jira_issue.get("key")
        jira_title   = jira_issue.get("summary")
        jira_updated = jira_issue.get("updated")

        print(f"\n  Processing: {issue_key} - '{jira_title}'")
        print(f"    JIRA status={jira_issue.get('status')} priority={jira_issue.get('priority')} "
              f"startDate={jira_issue.get('startDate')} endDate={jira_issue.get('endDate')} "
              f"timeSpent={jira_issue.get('timeSpent')}")

        try:
            jira_updated_dt = datetime.fromisoformat(jira_updated.replace("Z", "+00:00"))
            # Ensure timezone-aware; normalise to UTC
            if jira_updated_dt.tzinfo is None:
                jira_updated_dt = jira_updated_dt.replace(tzinfo=timezone.utc)
            else:
                jira_updated_dt = jira_updated_dt.astimezone(timezone.utc)
        except Exception:
            jira_updated_dt = datetime.now(timezone.utc)

        if jira_title in azure_by_title:
            azure_item = azure_by_title[jira_title]
            azure_id   = azure_item["id"]
            try:
                azure_changed_dt = datetime.fromisoformat(azure_item["changedDate"].replace("Z", "+00:00"))
                # Ensure timezone-aware; normalise to UTC
                if azure_changed_dt.tzinfo is None:
                    azure_changed_dt = azure_changed_dt.replace(tzinfo=timezone.utc)
                else:
                    azure_changed_dt = azure_changed_dt.astimezone(timezone.utc)
            except Exception:
                azure_changed_dt = datetime.min.replace(tzinfo=timezone.utc)

            print(f"    Found in Azure: id={azure_id} state={azure_item['state']}")
            print(f"    JIRA updated:   {jira_updated_dt}")
            print(f"    Azure changed:  {azure_changed_dt}")

            if jira_updated_dt > azure_changed_dt:
                existing_wit = azure_item.get("workItemType")
                print(f"    → JIRA is newer. UPDATING Azure item {azure_id} (type='{existing_wit}')...")
                try:
                    result = update_azure_work_item(
                        azure_org, azure_project, azure_team, azure_headers,
                        azure_id, jira_issue, state_cache, existing_wit
                    )
                    print(f"    ✓ Updated successfully: {result}")
                    sync_report["updated"].append({
                        "jira_key": issue_key, "jira_title": jira_title,
                        "azure_id": azure_id, "status": "success", "result": result
                    })
                except Exception as e:
                    print(f"    ✗ Update FAILED: {str(e)}")
                    sync_report["failed"].append({
                        "jira_key": issue_key, "jira_title": jira_title,
                        "azure_id": azure_id, "action": "UPDATE", "error": str(e)
                    })
            else:
                print(f"    → Azure is newer or equal. SKIPPING.")
                sync_report["skipped"].append({
                    "jira_key": issue_key, "jira_title": jira_title,
                    "azure_id": azure_id, "reason": "Azure is newer or equal"
                })
        else:
            print(f"    → Not found in Azure. CREATING Story + child Task...")
            try:
                result = create_azure_work_item(
                    azure_org, azure_project, azure_team, azure_headers, jira_issue, state_cache
                )
                print(f"    ✓ Created successfully: {result}")
                sync_report["created"].append({
                    "jira_key": issue_key, "jira_title": jira_title,
                    "status": "success", "result": result
                })
            except Exception as e:
                print(f"    ✗ Create FAILED: {str(e)}")
                sync_report["failed"].append({
                    "jira_key": issue_key, "jira_title": jira_title,
                    "action": "CREATE", "error": str(e)
                })

    print("\n" + "="*80)
    print(f"SYNC COMPLETE — Created:{len(sync_report['created'])} Updated:{len(sync_report['updated'])} "
          f"Skipped:{len(sync_report['skipped'])} Failed:{len(sync_report['failed'])}")
    print("="*80)

    return {
        "status": "completed",
        "timestamp": str(datetime.now()),
        "summary": {
            "total_jira_tickets": len(jira_issues),
            "total_azure_items":  len(azure_by_title),
            "created_count":  len(sync_report["created"]),
            "updated_count":  len(sync_report["updated"]),
            "skipped_count":  len(sync_report["skipped"]),
            "failed_count":   len(sync_report["failed"])
        },
        "details": sync_report
    }


# ============================================
# AZURE DEVOPS ENDPOINTS
# ============================================

@app.get("/azure/test-connection")
def test_azure_connection():
    azure_org     = os.getenv("AZURE_DEVOPS_ORG")
    azure_project = os.getenv("AZURE_DEVOPS_PROJECT")
    azure_team    = os.getenv("AZURE_DEVOPS_TEAM")
    azure_pat     = os.getenv("AZURE_DEVOPS_PAT")
    config_status = {
        "AZURE_DEVOPS_ORG":     "✓ Set" if azure_org     else "✗ Missing",
        "AZURE_DEVOPS_PROJECT": "✓ Set" if azure_project else "✗ Missing",
        "AZURE_DEVOPS_TEAM":    "✓ Set" if azure_team    else "✗ Missing",
        "AZURE_DEVOPS_PAT":     "✓ Set" if azure_pat     else "✗ Missing",
    }
    if not all([azure_org, azure_project, azure_pat]):
        return {"status": "error", "message": "Missing required environment variables", "config": config_status}
    auth_header = get_azure_auth_header()
    url = f"https://dev.azure.com/{azure_org}/_apis/projects?api-version=7.1"
    try:
        response = requests.get(url, headers={"Authorization": auth_header})
        return {
            "status": "success" if response.status_code == 200 else "error",
            "status_code": response.status_code,
            "url_tested": url,
            "config": config_status,
            "response_sample": response.json() if response.status_code == 200 else response.text
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "config": config_status}


@app.get("/azure/work-items")
def get_azure_work_items(limit: int = 100):
    azure_org     = os.getenv("AZURE_DEVOPS_ORG")
    azure_project = os.getenv("AZURE_DEVOPS_PROJECT")
    azure_team    = os.getenv("AZURE_DEVOPS_TEAM")
    if not all([azure_org, azure_project, azure_team]):
        return {"error": "Missing Azure DevOps credentials in environment variables."}
    auth_header = get_azure_auth_header()
    if not auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    if limit > 1000:
        limit = 1000
    headers = {"Content-Type": "application/json", "Authorization": auth_header}
    query = {
        "query": f"""SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType]
                     FROM WorkItems WHERE [System.TeamProject] = '{azure_project}'
                     AND [System.AreaPath] UNDER '{azure_project}\\{azure_team}'
                     ORDER BY [System.ChangedDate] DESC"""
    }
    try:
        response = requests.post(
            f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/wiql?api-version=7.1",
            headers=headers, json=query)
        response.raise_for_status()
        work_item_ids = [item["id"] for item in response.json().get("workItems", [])][:limit]
        if not work_item_ids:
            return {"work_items": [], "total_count": 0, "returned_count": 0}
        all_work_items = []
        for i in range(0, len(work_item_ids), 200):
            batch_ids = work_item_ids[i:i + 200]
            ids_str   = ",".join(map(str, batch_ids))
            det_resp  = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems?ids={ids_str}&$expand=all&api-version=7.1",
                headers=headers)
            det_resp.raise_for_status()
            for item in det_resp.json().get("value", []):
                all_work_items.append({
                    "id": item.get("id"), "rev": item.get("rev"), "url": item.get("url"),
                    "fields": item.get("fields", {}), "relations": item.get("relations", [])
                })
        return {
            "work_items":      all_work_items,
            "total_available": len(response.json().get("workItems", [])),
            "returned_count":  len(all_work_items)
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/azure/work-items/{work_item_id}")
def get_azure_work_item_details(work_item_id: int, include_updates: bool = False, include_comments: bool = False):
    azure_org     = os.getenv("AZURE_DEVOPS_ORG")
    azure_project = os.getenv("AZURE_DEVOPS_PROJECT")
    if not all([azure_org, azure_project]):
        return {"error": "Missing Azure DevOps credentials in environment variables."}
    auth_header = get_azure_auth_header()
    if not auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    headers = {"Authorization": auth_header}
    try:
        response = requests.get(
            f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{work_item_id}?$expand=all&api-version=7.1",
            headers=headers)
        response.raise_for_status()
        work_item_data = response.json()
        result = {
            "id":        work_item_data.get("id"),
            "rev":       work_item_data.get("rev"),
            "fields":    work_item_data.get("fields", {}),
            "relations": work_item_data.get("relations", []),
            "url":       work_item_data.get("url")
        }
        if include_updates:
            upd = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{work_item_id}/updates?api-version=7.1",
                headers=headers)
            if upd.status_code == 200:
                result["updates"] = upd.json().get("value", [])
        if include_comments:
            cmt = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{work_item_id}/comments?api-version=7.1-preview.3",
                headers=headers)
            if cmt.status_code == 200:
                result["comments"] = cmt.json().get("comments", [])
        return {"work_item": result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/azure/work-item-states")
def get_azure_work_item_states():
    """Debug: returns valid states per work item type for this Azure project."""
    azure_org     = os.getenv("AZURE_DEVOPS_ORG")
    azure_project = os.getenv("AZURE_DEVOPS_PROJECT")
    if not all([azure_org, azure_project]):
        return {"error": "Missing Azure DevOps credentials in environment variables."}
    auth_header = get_azure_auth_header()
    if not auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    headers = {"Authorization": auth_header}
    results = {}
    for work_item_type in ["User Story", "Task", "Bug", "Epic", "Feature"]:
        encoded = urllib.parse.quote(work_item_type)
        try:
            resp = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitemtypes/{encoded}/states?api-version=7.1",
                headers=headers)
            if resp.status_code == 200:
                results[work_item_type] = [
                    {"name": s.get("name"), "category": s.get("stateCategory"), "color": s.get("color")}
                    for s in resp.json().get("value", [])
                ]
            else:
                results[work_item_type] = {"error": resp.status_code, "detail": resp.text}
        except Exception as e:
            results[work_item_type] = {"error": str(e)}
    return {"project": azure_project, "valid_states_by_type": results}


@app.get("/azure/board-config")
def get_azure_board_config():
    azure_org     = os.getenv("AZURE_DEVOPS_ORG")
    azure_project = os.getenv("AZURE_DEVOPS_PROJECT")
    azure_team    = os.getenv("AZURE_DEVOPS_TEAM")
    if not all([azure_org, azure_project, azure_team]):
        return {"error": "Missing Azure DevOps credentials in environment variables."}
    auth_header = get_azure_auth_header()
    if not auth_header:
        return {"error": "Missing Azure DevOps PAT token"}
    team_encoded = urllib.parse.quote(azure_team)
    try:
        response = requests.get(
            f"https://dev.azure.com/{azure_org}/{azure_project}/{team_encoded}/_apis/work/boards/Stories/columns?api-version=7.1",
            headers={"Authorization": auth_header})
        response.raise_for_status()
        columns = [
            {"id": col.get("id"), "name": col.get("name"), "order": col.get("order"),
             "stateMappings": col.get("stateMappings", {})}
            for col in response.json().get("value", [])
        ]
        return {"columns": columns}
    except Exception as e:
        return {"error": str(e)}



# ============================================
# CONFIG-DRIVEN (DYNAMIC) SYNC ENGINE
# ============================================

def _make_azure_auth_header(pat: str) -> str:
    return f"Basic {base64.b64encode(f':{pat}'.encode()).decode()}"


def build_dynamic_patch(
    jira_issue: dict,
    area_path: str,
    available_states: list,
    mapping: MappingConfig,
    item_type: str = "Story",
    is_update: bool = False,
) -> list:
    title       = jira_issue.get("summary", "Untitled")
    jira_status = jira_issue.get("status", "To Do")
    jira_email  = jira_issue.get("assigneeEmail")

    rules = mapping.task_status_rules if item_type == "Task" else mapping.story_status_rules
    azure_state = resolve_state_generic(jira_status, available_states, rules, jira_email)

    priority_num, priority_label = resolve_priority_generic(
        jira_issue.get("priority", "Medium"), mapping.priority_rules
    )
    assignee_email = resolve_assignee_email_generic(jira_email, mapping.assignee_overrides)

    description_html = jira_issue.get("descriptionHtml") or ""
    if not description_html:
        plain = jira_issue.get("description") or ""
        description_html = f"<p>{plain}</p>" if plain else "<p>(No description provided in Jira.)</p>"

    time_spent_secs = jira_issue.get("timeSpent") or 0
    original_hours  = round(time_spent_secs / 3600, 2) if time_spent_secs else (1.0 if item_type == "Task" else 0.0)
    story_points    = map_hours_to_story_points(original_hours)

    op = "replace" if is_update else "add"
    patch = [
        {"op": op, "path": "/fields/System.Title",                               "value": title},
        {"op": op, "path": "/fields/System.Description",                         "value": description_html},
        {"op": op, "path": "/fields/System.State",                               "value": azure_state},
        {"op": op, "path": "/fields/Microsoft.VSTS.Common.Priority",             "value": priority_num},
        {"op": op, "path": "/fields/Custom.PriorityI",                           "value": priority_label},
        {"op": op, "path": "/fields/System.AreaPath",                            "value": area_path},
        {"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate", "value": original_hours},
    ]

    if item_type == "Story":
        patch.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StoryPoints", "value": story_points})
        patch.append({"op": op, "path": "/fields/Custom.StoryPointsI",                   "value": str(story_points)})
    else:
        patch.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.RemainingWork", "value": original_hours})

    start_date = jira_issue.get("startDate")
    if start_date:
        patch.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StartDate", "value": start_date})

    end_date = resolve_target_date(jira_issue)
    if end_date:
        patch.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.DueDate",    "value": end_date})
        patch.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate", "value": end_date})

    if assignee_email:
        patch.append({"op": op, "path": "/fields/System.AssignedTo", "value": assignee_email})

    return patch


def _dynamic_create_child_task(
    azure_org, azure_project, azure_team, azure_headers,
    story_id, jira_issue, task_states, mapping: MappingConfig
):
    area_path  = f"{azure_project}\\{azure_team}"
    task_patch = build_dynamic_patch(jira_issue, area_path, task_states, mapping, item_type="Task", is_update=False)
    task_url   = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/$Task?api-version=7.1"
    hdrs       = {**azure_headers, "Content-Type": "application/json-patch+json"}

    task_resp = requests.post(task_url, headers=hdrs, json=task_patch)
    if not task_resp.ok:
        if task_resp.status_code == 400:
            task_patch_stripped = [
                f for f in task_patch
                if f.get("path") not in ("/fields/Custom.PriorityI", "/fields/Custom.StoryPointsI")
            ]
            task_resp2 = requests.post(task_url, headers=hdrs, json=task_patch_stripped)
            if task_resp2.ok:
                print(f"    [CREATE TASK] Retry without custom fields succeeded")
                task_resp = task_resp2
            else:
                raise Exception(f"Azure {task_resp.status_code} creating Task — {task_resp.text[:800]}")
        else:
            raise Exception(f"Azure {task_resp.status_code} creating Task — {task_resp.text[:800]}")
    task_data = task_resp.json()
    task_id   = task_data.get("id")

    link_patch = [{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{story_id}",
            "attributes": {"comment": f"Child task synced from Jira {jira_issue.get('key', '')}"}
        }
    }]
    link_url  = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{task_id}?api-version=7.1"
    link_resp = requests.patch(link_url, headers=hdrs, json=link_patch)
    link_resp.raise_for_status()

    return {
        "task_id":            task_id,
        "task_url":           task_data.get("url"),
        "title":              task_data.get("fields", {}).get("System.Title"),
        "state":              task_data.get("fields", {}).get("System.State"),
        "linked_to_story_id": story_id,
    }


def _dynamic_create_work_item(
    azure_org, azure_project, azure_team, azure_headers,
    jira_issue, state_cache, mapping: MappingConfig
):
    area_path    = f"{azure_project}\\{azure_team}"
    story_states = state_cache.get("User Story", [])
    task_states  = state_cache.get("Task", [])

    story_patch = build_dynamic_patch(jira_issue, area_path, story_states, mapping, item_type="Story", is_update=False)
    url  = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/$User%20Story?api-version=7.1"
    hdrs = {**azure_headers, "Content-Type": "application/json-patch+json"}

    resp = requests.post(url, headers=hdrs, json=story_patch)
    if not resp.ok:
        # Retry without custom fields — they may not exist in every Azure project
        if resp.status_code == 400:
            story_patch_stripped = [
                f for f in story_patch
                if f.get("path") not in ("/fields/Custom.PriorityI", "/fields/Custom.StoryPointsI")
            ]
            resp2 = requests.post(url, headers=hdrs, json=story_patch_stripped)
            if resp2.ok:
                print(f"    [CREATE STORY] Retry without custom fields succeeded")
                resp = resp2
            else:
                raise Exception(f"Azure {resp.status_code} creating User Story — {resp.text[:800]}")
        else:
            raise Exception(f"Azure {resp.status_code} creating User Story — {resp.text[:800]}")
    created  = resp.json()
    story_id = created.get("id")

    task_result = None
    try:
        task_result = _dynamic_create_child_task(
            azure_org, azure_project, azure_team, azure_headers,
            story_id, jira_issue, task_states, mapping
        )
    except Exception as e:
        task_result = {"error": str(e)}

    return {
        "azure_id":   story_id,
        "azure_url":  created.get("url"),
        "title":      created.get("fields", {}).get("System.Title"),
        "state":      created.get("fields", {}).get("System.State"),
        "child_task": task_result,
    }


def _dynamic_update_work_item(
    azure_org, azure_project, azure_team, azure_headers,
    azure_id, jira_issue, state_cache, mapping: MappingConfig,
    existing_work_item_type: str = None,
):
    area_path = f"{azure_project}\\{azure_team}"
    sc = state_cache or {}

    if not existing_work_item_type:
        try:
            r = requests.get(
                f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{azure_id}?api-version=7.1",
                headers=azure_headers
            )
            if r.status_code == 200:
                existing_work_item_type = r.json().get("fields", {}).get("System.WorkItemType", "User Story")
            else:
                existing_work_item_type = "User Story"
        except Exception:
            existing_work_item_type = "User Story"

    story_states = sc.get(existing_work_item_type) or sc.get("User Story", [])
    task_states  = sc.get("Task", [])

    story_patch = build_dynamic_patch(jira_issue, area_path, story_states, mapping, item_type="Story", is_update=True)
    url  = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{azure_id}?api-version=7.1"
    hdrs = {**azure_headers, "Content-Type": "application/json-patch+json"}

    resp = requests.patch(url, headers=hdrs, json=story_patch)
    if not resp.ok:
        if resp.status_code == 400:
            story_patch_stripped = [
                f for f in story_patch
                if f.get("path") not in ("/fields/Custom.PriorityI", "/fields/Custom.StoryPointsI")
            ]
            resp2 = requests.patch(url, headers=hdrs, json=story_patch_stripped)
            if resp2.ok:
                print(f"    [UPDATE STORY] Retry without custom fields succeeded")
                resp = resp2
            else:
                raise Exception(f"Azure {resp.status_code} updating Story {azure_id} — {resp.text[:800]}")
        else:
            raise Exception(f"Azure {resp.status_code} updating Story {azure_id} — {resp.text[:800]}")
    updated = resp.json()

    task_result = None
    try:
        child_ids = get_child_task_ids_for_story(azure_org, azure_project, azure_headers, azure_id)
        if child_ids:
            updated_tasks = []
            for child_id in child_ids:
                try:
                    child_patch = build_dynamic_patch(jira_issue, area_path, task_states, mapping, item_type="Task", is_update=True)
                    child_url   = f"https://dev.azure.com/{azure_org}/{azure_project}/_apis/wit/workitems/{child_id}?api-version=7.1"
                    child_resp  = requests.patch(child_url, headers={**azure_headers, "Content-Type": "application/json-patch+json"}, json=child_patch)
                    if not child_resp.ok:
                        if child_resp.status_code == 400:
                            stripped = [f for f in child_patch if f.get("path") not in ("/fields/Custom.PriorityI", "/fields/Custom.StoryPointsI")]
                            child_resp = requests.patch(child_url, headers={**azure_headers, "Content-Type": "application/json-patch+json"}, json=stripped)
                        if not child_resp.ok:
                            raise Exception(f"Azure {child_resp.status_code} updating Task {child_id} — {child_resp.text[:800]}")
                    updated_tasks.append({"task_id": child_id, "action": "updated", "status": "success"})
                except Exception as e:
                    updated_tasks.append({"task_id": child_id, "action": "updated", "status": "error", "error": str(e)})
            task_result = {"updated_child_tasks": updated_tasks}
        else:
            task_result = _dynamic_create_child_task(
                azure_org, azure_project, azure_team, azure_headers,
                azure_id, jira_issue, task_states, mapping
            )
    except Exception as e:
        task_result = {"error": str(e)}

    return {
        "azure_id":   updated.get("id"),
        "azure_url":  updated.get("url"),
        "title":      updated.get("fields", {}).get("System.Title"),
        "state":      updated.get("fields", {}).get("System.State"),
        "child_task": task_result,
    }


def _run_sync_with_config(req: SyncRequest, dry_run: bool) -> dict:
    jira    = req.jira
    azure   = req.azure
    mapping = req.mapping

    # Safety gate: date range is always required — never allow an unbounded full migration
    df = getattr(req, "date_filter", None)
    if not df or (not df.from_date and not df.to_date):
        return {
            "status": "error",
            "error": (
                "Date range is required. All syncs must be date-bounded to prevent "
                "accidental migration of every Jira ticket into Azure DevOps. "
                "Please select a date range in the UI before syncing."
            ),
        }

    jira_issues = get_jira_issues_for_sync(
        jira.base_url, jira.email, jira.api_token, jira.project_key,
        date_filter=getattr(req, "date_filter", None),
    )

    auth_header   = _make_azure_auth_header(azure.pat)
    azure_headers = {"Content-Type": "application/json", "Authorization": auth_header}
    state_cache   = get_or_load_state_cache(azure.org, azure.project, auth_header)
    azure_by_title = get_azure_items_by_title(azure.org, azure.project, azure.team, azure_headers)

    report = {"created": [], "updated": [], "skipped": [], "failed": []}

    for jira_issue in jira_issues:
        issue_key  = jira_issue.get("key")
        jira_title = jira_issue.get("summary")
        try:
            jira_updated_dt = datetime.fromisoformat(
                (jira_issue.get("updated") or "").replace("Z", "+00:00")
            )
            jira_updated_dt = jira_updated_dt.astimezone(timezone.utc)
        except Exception:
            jira_updated_dt = datetime.now(timezone.utc)

        if jira_title in azure_by_title:
            azure_item = azure_by_title[jira_title]
            azure_id   = azure_item["id"]
            try:
                azure_changed_dt = datetime.fromisoformat(
                    (azure_item["changedDate"] or "").replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                azure_changed_dt = datetime.min.replace(tzinfo=timezone.utc)

            if jira_updated_dt > azure_changed_dt:
                if dry_run:
                    report["updated"].append({"action": "UPDATE", "jira_key": issue_key, "jira_title": jira_title, "azure_id": azure_id})
                else:
                    try:
                        result = _dynamic_update_work_item(
                            azure.org, azure.project, azure.team, azure_headers,
                            azure_id, jira_issue, state_cache, mapping,
                            existing_work_item_type=azure_item.get("workItemType"),
                        )
                        report["updated"].append({"jira_key": issue_key, "jira_title": jira_title, "azure_id": azure_id, "status": "success", "result": result})
                    except Exception as e:
                        report["failed"].append({"jira_key": issue_key, "jira_title": jira_title, "azure_id": azure_id, "action": "UPDATE", "error": str(e)})
            else:
                report["skipped"].append({"action": "SKIP", "jira_key": issue_key, "jira_title": jira_title, "azure_id": azure_id, "reason": "Azure is newer or equal"})
        else:
            if dry_run:
                report["created"].append({"action": "CREATE", "jira_key": issue_key, "jira_title": jira_title})
            else:
                try:
                    result = _dynamic_create_work_item(
                        azure.org, azure.project, azure.team, azure_headers,
                        jira_issue, state_cache, mapping
                    )
                    report["created"].append({"jira_key": issue_key, "jira_title": jira_title, "status": "success", "result": result})
                except Exception as e:
                    report["failed"].append({"jira_key": issue_key, "jira_title": jira_title, "action": "CREATE", "error": str(e)})

    df = getattr(req, "date_filter", None)
    applied_filter = None
    if df and (df.from_date or df.to_date):
        applied_filter = {
            "field":     df.field,
            "from_date": df.from_date,
            "to_date":   df.to_date,
        }

    return {
        "status":    "preview" if dry_run else "completed",
        "is_dry_run": dry_run,
        "timestamp": str(datetime.now()),
        "date_filter_applied": applied_filter,
        "summary": {
            "total_jira_tickets": len(jira_issues),
            "total_azure_items":  len(azure_by_title),
            "created_count":  len(report["created"]),
            "updated_count":  len(report["updated"]),
            "skipped_count":  len(report["skipped"]),
            "failed_count":   len(report["failed"]),
        },
        "details": report,
    }


# ============================================
# CONFIG-DRIVEN API ENDPOINTS  (used by the UI)
# ============================================

@app.get("/api/default-config")
def api_default_config():
    """Return the built-in default mapping configuration."""
    return default_mapping_config().model_dump()


@app.post("/api/test-connection")
def api_test_connection(req: SyncRequest):
    """Validate Jira + Azure credentials supplied in the request body."""
    errors = []

    # Jira
    try:
        r = requests.get(
            f"{req.jira.base_url}/rest/api/3/myself",
            auth=(req.jira.email, req.jira.api_token),
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            jira_user = r.json().get("displayName", "unknown")
            jira_ok   = True
        else:
            jira_ok   = False
            jira_user = None
            errors.append(f"Jira auth failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        jira_ok   = False
        jira_user = None
        errors.append(f"Jira connection error: {e}")

    # Azure
    auth_header = _make_azure_auth_header(req.azure.pat)
    try:
        r = requests.get(
            f"https://dev.azure.com/{req.azure.org}/_apis/projects/{req.azure.project}?api-version=7.1",
            headers={"Authorization": auth_header},
            timeout=10,
        )
        if r.status_code == 200:
            azure_ok   = True
            azure_proj = r.json().get("name", "unknown")
        else:
            azure_ok   = False
            azure_proj = None
            errors.append(f"Azure auth failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        azure_ok   = False
        azure_proj = None
        errors.append(f"Azure connection error: {e}")

    return {
        "jira_ok":     jira_ok,
        "jira_user":   jira_user,
        "azure_ok":    azure_ok,
        "azure_project": azure_proj,
        "errors":      errors,
        "all_ok":      jira_ok and azure_ok,
    }


@app.get("/api/azure-states")
def api_azure_states(req: SyncRequest):
    """Return valid Azure states for User Story and Task in the given project."""
    auth_header = _make_azure_auth_header(req.azure.pat)
    cache = get_or_load_state_cache(req.azure.org, req.azure.project, auth_header)
    return {"states": cache}


@app.post("/api/sync/preview")
def api_sync_preview(req: SyncRequest):
    """Dry-run sync using the supplied config — returns what would be created/updated."""
    try:
        return _run_sync_with_config(req, dry_run=True)
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/sync/run")
def api_sync_run(req: SyncRequest):
    """Live sync using the supplied config."""
    try:
        return _run_sync_with_config(req, dry_run=False)
    except Exception as e:
        return {"status": "error", "error": str(e)}