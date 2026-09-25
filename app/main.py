from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
import os
import re
import sys
import html
import socket
import pathlib
import ipaddress
import requests
from datetime import date, datetime, timedelta, timezone
import base64
import urllib.parse
import json

from app.mapping import (
    MappingConfig, SyncRequest,
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
            f"https://dev.azure.com/{urllib.parse.quote(azure_org, safe='')}/{urllib.parse.quote(azure_project, safe='')}/_apis/wit/workitemtypes?api-version=7.1",
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
                f"https://dev.azure.com/{urllib.parse.quote(azure_org, safe='')}/{urllib.parse.quote(azure_project, safe='')}/_apis/wit/workitemtypes/{encoded}/states?api-version=7.1",
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
    key = f"{azure_org}/{azure_project}".lower()
    if key not in _azure_state_cache:
        print(f"  [STATE CACHE] Fetching valid states from Azure project '{azure_project}'...")
        states = fetch_azure_states_for_project(azure_org, azure_project, auth_header)
        if not any(states.values()):
            return states  # lookup failed, so don't cache it; the next sync retries
        _azure_state_cache[key] = states
    return _azure_state_cache[key]


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
# REQUEST SAFETY & VALIDATION
# ============================================

HTTP_TIMEOUT          = 30        # seconds — applied to every Jira / Azure call
MAX_DESCRIPTION_CHARS = 500_000   # larger HTML (e.g. many embedded images) falls back to plain text
MAX_JIRA_PAGES        = 100       # 100 pages x 100 issues — hard stop for runaway pagination
STORY_TAG_PREFIX      = "jira:"       # tag on each Azure story, e.g. "jira:PROJ-12"
TASK_TAG_PREFIX       = "jira-task:"  # tag on the child task this tool creates
CUSTOM_FIELDS         = ("Custom.PriorityI", "Custom.StoryPointsI")


def _q(segment: str) -> str:
    """URL-encode one path segment (org / project / team names can contain spaces)."""
    return urllib.parse.quote(segment, safe="")


def _az_base(org: str, project: str) -> str:
    return f"https://dev.azure.com/{_q(org)}/{_q(project)}"


def _az_web_url(org: str, project: str, work_item_id) -> str:
    return f"{_az_base(org, project)}/_workitems/edit/{work_item_id}"


def _is_private_host(hostname: str) -> bool:
    """True if the host resolves to a loopback / private / link-local address."""
    try:
        infos = socket.getaddrinfo(hostname, 443)
    except socket.gaierror:
        return False  # unresolvable — the request itself will fail with a clear error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _validate_request(req: SyncRequest) -> str | None:
    """Normalise the request in place. Returns an error message, or None when valid."""
    j, a = req.jira, req.azure
    for model, name in ((j, "base_url"), (j, "email"), (j, "api_token"), (j, "project_key"),
                        (a, "org"), (a, "project"), (a, "team"), (a, "pat")):
        setattr(model, name, (getattr(model, name) or "").strip())

    required = [
        ("Jira base URL", j.base_url), ("Jira email", j.email), ("Jira API token", j.api_token),
        ("Jira project key", j.project_key), ("Azure organisation", a.org), ("Azure project", a.project),
        ("Azure team", a.team), ("Azure personal access token", a.pat),
    ]
    for label, value in required:
        if not value:
            return f"{label} is required."

    j.base_url = j.base_url.rstrip("/")
    parsed = urllib.parse.urlparse(j.base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "Jira base URL must start with https:// (for example https://your-domain.atlassian.net)."
    if parsed.path not in ("", "/") or parsed.query:
        return "Jira base URL should be just the site address, for example https://your-domain.atlassian.net."
    if _is_private_host(parsed.hostname):
        return "Jira base URL must be a public Jira Cloud address."

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", j.project_key):
        return "Jira project key can only contain letters, numbers and underscores (for example PROJ)."
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.org):
        return "Azure organisation can only contain letters, numbers, dots, dashes and underscores."
    for label, value in (("Azure project", a.project), ("Azure team", a.team)):
        if any(c in value for c in '/\\?#%\''):
            return f"{label} contains characters that aren't allowed (/ \\ ? # % ')."
    return None


def _make_azure_auth_header(pat: str) -> str:
    return f"Basic {base64.b64encode(f':{pat}'.encode()).decode()}"


# ============================================
# JIRA FETCHING
# ============================================

JIRA_SYNC_FIELDS = ["summary", "status", "assignee", "priority", "created", "updated",
                    "duedate", "startdate", "customfield_10015", "description", "timespent"]


def fetch_all_jira_issues(jira_base_url, jira_email, jira_token, jql, fields):
    """
    The Jira Cloud /rest/api/3/search/jql endpoint paginates via nextPageToken.
    Keep requesting pages until isLast (or no nextPageToken).
    """
    auth    = (jira_email, jira_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    url     = f"{jira_base_url}/rest/api/3/search/jql"

    all_issues, next_token, seen_tokens = [], None, set()
    for _ in range(MAX_JIRA_PAGES):
        payload = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_token:
            payload["nextPageToken"] = next_token

        response = requests.post(url, headers=headers, json=payload, auth=auth, timeout=HTTP_TIMEOUT)
        if response.status_code in (401, 403):
            raise Exception(f"Jira rejected the credentials (HTTP {response.status_code}). Check the email and API token.")
        if response.status_code == 400:
            raise Exception(f"Jira rejected the search — check the project key. Details: {response.text[:300]}")
        response.raise_for_status()
        data = response.json()

        page_issues = data.get("issues", [])
        all_issues.extend(page_issues)

        next_token = data.get("nextPageToken")
        if data.get("isLast", not next_token) or not next_token or not page_issues or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
    return all_issues


def _parse_iso_date(value, label):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid {label} '{value}' — expected YYYY-MM-DD.")


def _build_date_jql_clause(date_filter) -> str:
    """
    JQL clause (no leading AND) for the DateFilter. In JQL a bare date means midnight at the
    START of that day, so the end of the range uses '< next day' to include the whole To date.
    """
    if not date_filter:
        return ""
    field = (date_filter.field or "updated").strip().lower()
    if field not in ("updated", "created", "either"):
        raise ValueError(f"Unknown date filter field '{field}'.")
    from_d = _parse_iso_date(date_filter.from_date, "From date")
    to_d   = _parse_iso_date(date_filter.to_date, "To date")
    if not from_d and not to_d:
        return ""
    if from_d and to_d and from_d > to_d:
        raise ValueError("The From date must be on or before the To date.")

    def rng(f):
        parts = []
        if from_d:
            parts.append(f'{f} >= "{from_d.isoformat()}"')
        if to_d:
            parts.append(f'{f} < "{(to_d + timedelta(days=1)).isoformat()}"')
        return " AND ".join(parts)

    if field == "either":
        return f"({rng('updated')}) OR ({rng('created')})"
    return rng(field)


def search_jira_issues_for_sync(jira, date_filter):
    date_clause = _build_date_jql_clause(date_filter)
    jql = f'project = "{jira.project_key}"'
    if date_clause:
        jql += f" AND ({date_clause})"
    jql += " ORDER BY created DESC"
    print(f"  [JIRA SYNC] JQL: {jql}")
    return fetch_all_jira_issues(jira.base_url, jira.email, jira.api_token, jql, JIRA_SYNC_FIELDS)


def normalize_jira_issue(issue, jira):
    """Turn a raw Jira search result into the flat dict the sync engine uses."""
    fields    = issue.get("fields") or {}
    issue_key = issue.get("key")
    raw_desc  = fields.get("description")
    creds     = {"base_url": jira.base_url, "email": jira.email, "token": jira.api_token, "issue_key": issue_key}

    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    status   = fields.get("status") or {}

    return {
        "key":             issue_key,
        "summary":         (fields.get("summary") or "").strip() or "Untitled",
        "description":     adf_to_plain_text(raw_desc),
        "descriptionHtml": adf_to_html(raw_desc, jira_creds=creds),
        "status":          status.get("name"),
        "priority":        priority.get("name"),
        "assignee":        assignee.get("displayName"),
        "assigneeEmail":   assignee.get("emailAddress"),
        "created":         fields.get("created"),
        "updated":         fields.get("updated"),
        "startDate":       fields.get("customfield_10015") or fields.get("startdate") or fields.get("created"),
        "endDate":         get_qa_date_from_changelog(issue_key, jira.base_url, jira.email, jira.api_token),
        "dueDate":         fields.get("duedate"),
        "timeSpent":       fields.get("timespent"),
    }


# ============================================
# AZURE DEVOPS READS
# ============================================

def fetch_azure_work_items_by_ids(azure_org, azure_project, azure_headers, work_item_ids):
    all_items = []
    for i in range(0, len(work_item_ids), 200):
        ids_str  = ",".join(str(wid) for wid in work_item_ids[i:i + 200])
        url      = f"{_az_base(azure_org, azure_project)}/_apis/wit/workitems?ids={ids_str}&errorPolicy=omit&api-version=7.1"
        response = requests.get(url, headers=azure_headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        all_items.extend(v for v in response.json().get("value", []) if v)
    return all_items


def _split_tags(tags_field) -> list:
    return [t.strip() for t in (tags_field or "").split(";") if t.strip()]


def _tagged_keys(tags: list, prefix: str) -> set:
    return {t[len(prefix):].strip().upper() for t in tags if t.lower().startswith(prefix)}


def _to_item(raw: dict) -> dict:
    f = raw.get("fields", {}) or {}
    return {
        "id":          raw.get("id"),
        "type":        f.get("System.WorkItemType"),
        "title":       f.get("System.Title"),
        "changedDate": f.get("System.ChangedDate"),
        "tags":        _split_tags(f.get("System.Tags")),
        "fields":      f,
    }


def get_azure_index(azure_org, azure_project, azure_team, azure_headers):
    """
    Index work items under the team's area path.
      by_key   — stories tagged "jira:KEY" (the reliable match)
      by_title — untagged, non-Task items by title (fallback for items synced before tagging existed)
    """
    wiql_url   = f"{_az_base(azure_org, azure_project)}/_apis/wit/wiql?api-version=7.1"
    area_path  = f"{azure_project}\\{azure_team}".replace("'", "''")
    wiql_query = {
        "query": f"""SELECT [System.Id] FROM WorkItems
                     WHERE [System.TeamProject] = '{azure_project.replace("'", "''")}'
                     AND [System.AreaPath] UNDER '{area_path}'
                     ORDER BY [System.ChangedDate] DESC"""
    }
    resp = requests.post(wiql_url, headers=azure_headers, json=wiql_query, timeout=HTTP_TIMEOUT)
    if resp.status_code in (401, 403):
        raise Exception(f"Azure DevOps rejected the personal access token (HTTP {resp.status_code}).")
    if resp.status_code == 400:
        raise Exception(f"Azure DevOps couldn't query the team's area path — check the project and team names. Details: {resp.text[:300]}")
    resp.raise_for_status()
    ids   = [w.get("id") for w in resp.json().get("workItems", [])]
    items = [_to_item(r) for r in fetch_azure_work_items_by_ids(azure_org, azure_project, azure_headers, ids)] if ids else []

    by_key, by_title = {}, {}
    for it in items:
        if it["type"] == "Task":
            continue
        keys = _tagged_keys(it["tags"], STORY_TAG_PREFIX)
        if keys:
            for k in keys:
                by_key.setdefault(k, it)   # newest first (WIQL order)
        elif it["title"] and not _tagged_keys(it["tags"], TASK_TAG_PREFIX):
            by_title.setdefault(it["title"], []).append(it)
    print(f"  [AZURE] {len(items)} work items under area path — {len(by_key)} linked by Jira key")
    return {"count": len(items), "by_key": by_key, "by_title": by_title}


def get_child_tasks(azure_org, azure_project, azure_headers, story_id) -> list:
    url  = f"{_az_base(azure_org, azure_project)}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=7.1"
    resp = requests.get(url, headers=azure_headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    child_ids = []
    for rel in resp.json().get("relations", []) or []:
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
            try:
                child_ids.append(int(rel.get("url", "").rstrip("/").split("/")[-1]))
            except ValueError:
                pass
    if not child_ids:
        return []
    raw = fetch_azure_work_items_by_ids(azure_org, azure_project, azure_headers, child_ids)
    return [c for c in (_to_item(r) for r in raw) if c["type"] == "Task"]


# ============================================
# FIELD MAPPING & CHANGE DETECTION
# ============================================

FIELD_LABELS = {
    "System.Title":                               "Title",
    "System.Description":                         "Description",
    "System.State":                               "State",
    "Microsoft.VSTS.Common.Priority":             "Priority",
    "Custom.PriorityI":                           "Priority (custom)",
    "System.AreaPath":                            "Area path",
    "Microsoft.VSTS.Scheduling.OriginalEstimate": "Original estimate (hours)",
    "Microsoft.VSTS.Scheduling.StoryPoints":      "Story points",
    "Custom.StoryPointsI":                        "Story points (custom)",
    "Microsoft.VSTS.Scheduling.RemainingWork":    "Remaining work (hours)",
    "Microsoft.VSTS.Scheduling.StartDate":        "Start date",
    "Microsoft.VSTS.Scheduling.DueDate":          "Due date",
    "Microsoft.VSTS.Scheduling.TargetDate":       "Target date",
    "System.AssignedTo":                          "Assigned to",
    "System.Tags":                                "Tags",
}
DATE_FIELDS     = {"Microsoft.VSTS.Scheduling.StartDate", "Microsoft.VSTS.Scheduling.DueDate",
                   "Microsoft.VSTS.Scheduling.TargetDate"}
NUMERIC_FIELDS  = {"Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.Scheduling.OriginalEstimate",
                   "Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.RemainingWork"}
ESTIMATE_FIELDS = {"Microsoft.VSTS.Scheduling.OriginalEstimate", "Microsoft.VSTS.Scheduling.StoryPoints",
                   "Custom.StoryPointsI", "Microsoft.VSTS.Scheduling.RemainingWork"}


def map_hours_to_story_points(hours: float) -> int:
    FIBONACCI_POINTS = [1, 2, 3, 5, 8, 13, 21]
    if not hours or hours <= 0:
        return 1
    return min(FIBONACCI_POINTS, key=lambda sp: abs(sp - hours))


def resolve_target_date(jira_issue) -> str | None:
    """First QA transition in Jira; falls back to Jira's Due Date. Dateless tickets stay unset."""
    return jira_issue.get("endDate") or jira_issue.get("dueDate") or None


def _html_to_text(value) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _plain_description_html(jira_issue) -> str:
    plain = jira_issue.get("description") or ""
    if not plain:
        return "<p>(No description provided in Jira.)</p>"
    return "".join(f"<p>{html.escape(line)}</p>" for line in plain.splitlines() if line.strip()) or "<p></p>"


def desired_fields(jira_issue, area_path, available_states, mapping: MappingConfig, item_type: str, tag: str) -> dict:
    """The Azure field values this Jira ticket should produce."""
    rules       = mapping.task_status_rules if item_type == "Task" else mapping.story_status_rules
    jira_email  = jira_issue.get("assigneeEmail")
    azure_state = resolve_state_generic(jira_issue.get("status") or "To Do", available_states, rules, jira_email)
    priority_num, priority_label = resolve_priority_generic(jira_issue.get("priority") or "Medium", mapping.priority_rules)
    assignee_email = resolve_assignee_email_generic(jira_email, mapping.assignee_overrides)

    description_html = jira_issue.get("descriptionHtml") or ""
    if not description_html or len(description_html) > MAX_DESCRIPTION_CHARS:
        description_html = _plain_description_html(jira_issue)

    time_spent_secs = jira_issue.get("timeSpent") or 0
    hours = round(time_spent_secs / 3600, 2) if time_spent_secs else (1.0 if item_type == "Task" else 0.0)

    f = {
        "System.Title":                               jira_issue.get("summary") or "Untitled",
        "System.Description":                         description_html,
        "System.State":                               azure_state,
        "Microsoft.VSTS.Common.Priority":             priority_num,
        "Custom.PriorityI":                           priority_label,
        "System.AreaPath":                            area_path,
        "Microsoft.VSTS.Scheduling.OriginalEstimate": hours,
        "System.Tags":                                tag,
    }
    if item_type == "Story":
        points = map_hours_to_story_points(hours)
        f["Microsoft.VSTS.Scheduling.StoryPoints"] = points
        f["Custom.StoryPointsI"] = str(points)
    else:
        f["Microsoft.VSTS.Scheduling.RemainingWork"] = hours

    if jira_issue.get("startDate"):
        f["Microsoft.VSTS.Scheduling.StartDate"] = jira_issue["startDate"]
    end_date = resolve_target_date(jira_issue)
    if end_date:
        f["Microsoft.VSTS.Scheduling.DueDate"]    = end_date
        f["Microsoft.VSTS.Scheduling.TargetDate"] = end_date
    if assignee_email:
        f["System.AssignedTo"] = assignee_email
    return f


def _display(field, value):
    if value is None or value == "":
        return "—"
    if field == "System.AssignedTo":
        if isinstance(value, dict):
            name, email = value.get("displayName"), value.get("uniqueName")
            return f"{name} <{email}>" if name and email else (name or email or "—")
        return str(value)
    if field in DATE_FIELDS:
        return str(value)[:10]
    if field == "System.Description":
        text = _html_to_text(value)
        return (text[:140] + "…") if len(text) > 140 else (text or "(empty)")
    return str(value)


def _same(field, current, desired) -> bool:
    if current is None or current == "":
        return desired is None or desired == ""
    if field == "System.AssignedTo":
        cur = current.get("uniqueName") if isinstance(current, dict) else current
        return str(cur or "").strip().lower() == str(desired or "").strip().lower()
    if field in DATE_FIELDS:
        return str(current)[:10] == str(desired)[:10]
    if field in NUMERIC_FIELDS:
        try:
            return abs(float(current) - float(desired)) < 0.01
        except (TypeError, ValueError):
            return False
    if field == "System.Description":
        return _html_to_text(current) == _html_to_text(desired)
    if field == "System.Tags":
        return {t.lower() for t in _split_tags(current)} == {t.lower() for t in _split_tags(desired)}
    return str(current).strip().lower() == str(desired).strip().lower()


def diff_for_update(item: dict, desired: dict, jira_issue) -> tuple:
    """
    Only fields that actually differ are sent, so untouched Azure fields keep any manual edits.
    Area path is never moved on update, estimates are left alone when Jira has no time logged,
    custom fields are only touched if the item already has them, and tags are merged, not replaced.
    """
    current = item["fields"]
    want    = dict(desired)
    want.pop("System.AreaPath", None)
    if not jira_issue.get("timeSpent"):
        for k in ESTIMATE_FIELDS:
            want.pop(k, None)
    for k in CUSTOM_FIELDS:
        if k not in current:
            want.pop(k, None)
    merged = item["tags"] + [t for t in _split_tags(want.get("System.Tags")) if t.lower() not in {x.lower() for x in item["tags"]}]
    want["System.Tags"] = "; ".join(merged)

    patch, changes = {}, []
    for field, value in want.items():
        if not _same(field, current.get(field), value):
            patch[field] = value
            changes.append({"field": FIELD_LABELS.get(field, field),
                            "from": _display(field, current.get(field)), "to": _display(field, value)})
    return patch, changes


def changes_for_create(desired: dict) -> list:
    return [{"field": FIELD_LABELS.get(k, k), "from": None, "to": _display(k, v)} for k, v in desired.items()]


# ============================================
# AZURE DEVOPS WRITES
# ============================================

def _write_work_item(method, url, azure_headers, fields: dict, jira_issue, extra_ops=None) -> tuple:
    """
    Create or update a work item. On HTTP 400 it retries, first without the optional custom
    fields (they don't exist in every project), then also with a plain-text description.
    Returns (response_json, notes).
    """
    hdrs = {**azure_headers, "Content-Type": "application/json-patch+json"}
    no_custom = {k: v for k, v in fields.items() if k not in CUSTOM_FIELDS}
    attempts  = [(fields, None)]
    if len(no_custom) != len(fields):
        attempts.append((no_custom, "Custom priority / story point fields don't exist in this project — skipped them."))
    if "System.Description" in no_custom:
        attempts.append(({**no_custom, "System.Description": _plain_description_html(jira_issue)},
                         "Azure rejected the formatted description — sent it as plain text instead."))

    notes, last = [], None
    for body_fields, note in attempts:
        ops  = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in body_fields.items()]
        ops += extra_ops or []
        resp = requests.request(method, url, headers=hdrs, json=ops, timeout=HTTP_TIMEOUT)
        if resp.ok:
            if note:
                notes.append(note)
            return resp.json(), notes
        last = resp
        if resp.status_code != 400:
            break
    detail = last.text[:600] if last is not None else ""
    try:
        detail = last.json().get("message", detail)
    except Exception:
        pass
    raise Exception(f"Azure DevOps returned HTTP {last.status_code}: {detail}")


def create_story(azure, azure_headers, fields, jira_issue):
    url = f"{_az_base(azure.org, azure.project)}/_apis/wit/workitems/$User%20Story?api-version=7.1"
    return _write_work_item("POST", url, azure_headers, fields, jira_issue)


def create_child_task(azure, azure_headers, story_id, fields, jira_issue):
    url  = f"{_az_base(azure.org, azure.project)}/_apis/wit/workitems/$Task?api-version=7.1"
    link = [{
        "op": "add", "path": "/relations/-",
        "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{_az_base(azure.org, azure.project)}/_apis/wit/workitems/{story_id}",
            "attributes": {"comment": f"Child task synced from Jira {jira_issue.get('key', '')}"},
        },
    }]
    # The parent link is part of the same request, so a task is never left unlinked.
    return _write_work_item("POST", url, azure_headers, fields, jira_issue, extra_ops=link)


def update_item(azure, azure_headers, item_id, fields, jira_issue):
    url = f"{_az_base(azure.org, azure.project)}/_apis/wit/workitems/{item_id}?api-version=7.1"
    return _write_work_item("PATCH", url, azure_headers, fields, jira_issue)


# ============================================
# SYNC ENGINE (streams one event per step)
# ============================================

def _parse_dt(value, default):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return default


def _sync_child_tasks(azure, azure_headers, story_id, story_title, jira_issue, task_states, mapping, dry_run):
    """Sync the child task this tool owns. Tasks someone added by hand are never touched."""
    key       = jira_issue["key"]
    area_path = f"{azure.project}\\{azure.team}"
    task_tag  = f"{TASK_TAG_PREFIX}{key}"
    desired   = desired_fields(jira_issue, area_path, task_states, mapping, "Task", task_tag)

    children = get_child_tasks(azure.org, azure.project, azure_headers, story_id) if story_id else []
    owned    = [c for c in children if key in _tagged_keys(c["tags"], TASK_TAG_PREFIX)]
    if not owned:
        # Tasks created before tagging existed carry the story's title and no task tag.
        titles = {t for t in (story_title, jira_issue.get("summary")) if t}
        owned  = [c for c in children if c["title"] in titles and not _tagged_keys(c["tags"], TASK_TAG_PREFIX)]

    if not owned:
        if children:
            return [{"action": "untouched", "task_id": None,
                     "note": f"{len(children)} existing child task(s) weren't created by this tool — left untouched."}]
        if dry_run:
            return [{"action": "create", "task_id": None, "changes": changes_for_create(desired)}]
        created, notes = create_child_task(azure, azure_headers, story_id, desired, jira_issue)
        return [{"action": "create", "task_id": created.get("id"),
                 "url": _az_web_url(azure.org, azure.project, created.get("id")),
                 "changes": changes_for_create(desired), "notes": notes}]

    results = []
    for task in owned:
        patch, changes = diff_for_update(task, desired, jira_issue)
        entry = {"task_id": task["id"], "url": _az_web_url(azure.org, azure.project, task["id"])}
        if not patch:
            results.append({**entry, "action": "unchanged", "changes": []})
            continue
        if dry_run:
            results.append({**entry, "action": "update", "changes": changes})
            continue
        try:
            _, notes = update_item(azure, azure_headers, task["id"], patch, jira_issue)
            results.append({**entry, "action": "update", "changes": changes, "notes": notes})
        except Exception as e:
            results.append({**entry, "action": "fail", "changes": changes, "error": str(e)})
    return results


def _process_ticket(raw_issue, ctx):
    """
    Sync one Jira ticket. A generator: yields live 'step' events and returns the result dict.
    Never raises — any error becomes a 'fail' result for this ticket only.
    """
    req, dry_run, index = ctx["req"], ctx["dry_run"], ctx["index"]
    azure, mapping      = req.azure, req.mapping
    key   = raw_issue.get("key") or "?"
    title = ((raw_issue.get("fields") or {}).get("summary") or "").strip() or "Untitled"
    result = {"key": key, "title": title, "action": None, "changes": [], "tasks": [], "notes": []}

    def step(message):
        return {"type": "step", "index": index, "message": message}

    try:
        yield step("Reading ticket details from Jira")
        jira_issue = normalize_jira_issue(raw_issue, req.jira)
        result["jira_status"] = jira_issue.get("status")

        area_path = f"{azure.project}\\{azure.team}"
        story_tag = f"{STORY_TAG_PREFIX}{key}"
        desired   = desired_fields(jira_issue, area_path, ctx["story_states"], mapping, "Story", story_tag)

        yield step("Looking for a matching Azure work item")
        item, match = ctx["by_key"].get(key.upper()), "jira key"
        if not item:
            candidates = [c for c in ctx["by_title"].get(jira_issue["summary"], []) if c["id"] not in ctx["claimed"]]
            if len(candidates) > 1:
                result.update(action="skip", reason=(
                    f"{len(candidates)} Azure items share this title and none is linked to {key}. Skipped so the "
                    f"wrong one isn't overwritten. Add the tag '{story_tag}' to the right item in Azure to link it."))
                return result
            if candidates:
                item, match = candidates[0], "title"
        if item:
            ctx["claimed"].add(item["id"])

        # ── New story ────────────────────────────────────────────────
        if not item:
            result["action"]  = "create"
            result["changes"] = changes_for_create(desired)
            if dry_run:
                task_desired = desired_fields(jira_issue, area_path, ctx["task_states"], mapping, "Task",
                                              f"{TASK_TAG_PREFIX}{key}")
                result["tasks"] = [{"action": "create", "task_id": None, "changes": changes_for_create(task_desired)}]
                return result
            yield step("Creating User Story in Azure DevOps")
            created, notes = create_story(azure, ctx["headers"], desired, jira_issue)
            story_id = created.get("id")
            result.update(azure_id=story_id, azure_url=_az_web_url(azure.org, azure.project, story_id))
            result["notes"] += notes
            yield step(f"Created story #{story_id}. Creating its child task")
            try:
                result["tasks"] = _sync_child_tasks(azure, ctx["headers"], story_id, None, jira_issue,
                                                    ctx["task_states"], mapping, dry_run)
            except Exception as e:
                result["tasks"] = [{"action": "fail", "task_id": None, "error": str(e)}]
            return result

        # ── Existing story ───────────────────────────────────────────
        story_id = item["id"]
        result.update(azure_id=story_id, azure_url=_az_web_url(azure.org, azure.project, story_id), match=match)
        yield step(f"Comparing Jira with Azure #{story_id}")
        patch, changes = diff_for_update(item, desired, jira_issue)

        jira_updated  = _parse_dt(jira_issue.get("updated"), datetime.now(timezone.utc))
        azure_changed = _parse_dt(item.get("changedDate"), datetime.min.replace(tzinfo=timezone.utc))
        azure_is_newer = azure_changed > jira_updated
        if patch and azure_is_newer:
            when = (f"The Azure item was edited ({azure_changed:%Y-%m-%d %H:%M} UTC) after the Jira ticket's last "
                    f"update ({jira_updated:%Y-%m-%d %H:%M} UTC)")
            if match == "jira key":
                result.update(action="skip", changes=changes, reason=(
                    f"{when}. Skipped so those Azure edits aren't overwritten. The differences are listed below."))
                return result
            # Matched by title: only add the link tag, keep Azure's field values.
            skipped = [c for c in changes if c["field"] != "Tags"]
            patch   = {k: v for k, v in patch.items() if k == "System.Tags"}
            changes = [c for c in changes if c["field"] == "Tags"]
            if skipped:
                result["notes"].append(f"{when}, so only the Jira link tag is added. "
                                       f"{len(skipped)} other difference(s) were left as they are in Azure.")

        if patch and not dry_run:
            yield step(f"Updating {len(changes)} field(s) on #{story_id}")
            _, notes = update_item(azure, ctx["headers"], story_id, patch, jira_issue)
            result["notes"] += notes
        result["changes"] = changes

        yield step(f"Checking child tasks of #{story_id}")
        try:
            result["tasks"] = _sync_child_tasks(azure, ctx["headers"], story_id, item["title"], jira_issue,
                                                ctx["task_states"], mapping, dry_run)
        except Exception as e:
            result["tasks"] = [{"action": "fail", "task_id": None, "error": str(e)}]

        task_changed = any(t.get("action") in ("create", "update", "fail") for t in result["tasks"])
        if patch or task_changed:
            result["action"] = "update"
        else:
            result.update(action="skip", reason="Already up to date. Nothing differs between Jira and Azure.")
        return result

    except requests.exceptions.Timeout:
        result.update(action="fail", error=f"Jira or Azure DevOps didn't respond within {HTTP_TIMEOUT} seconds.")
        return result
    except Exception as e:
        result.update(action="fail", error=str(e))
        return result
    finally:
        result["has_task_errors"] = any(t.get("action") == "fail" for t in result["tasks"])


def sync_events(req: SyncRequest, dry_run: bool):
    """Generator of progress events. The last event is always 'done' or 'error'."""
    mode = "preview" if dry_run else "live"
    try:
        error = _validate_request(req)
        if error:
            yield {"type": "error", "error": error}
            return

        df = req.date_filter
        if not df or (not (df.from_date or "").strip() and not (df.to_date or "").strip()):
            yield {"type": "error", "error": ("Date range is required. All syncs must be date-bounded to prevent "
                                               "accidental migration of every Jira ticket into Azure DevOps.")}
            return
        try:
            _build_date_jql_clause(df)
        except ValueError as e:
            yield {"type": "error", "error": str(e)}
            return

        yield {"type": "stage", "message": f"Searching Jira project {req.jira.project_key} for tickets in the date range"}
        raw_issues = search_jira_issues_for_sync(req.jira, df)

        yield {"type": "stage", "message": f"Found {len(raw_issues)} Jira ticket(s). Reading Azure DevOps board"}
        auth_header = _make_azure_auth_header(req.azure.pat)
        headers     = {"Content-Type": "application/json", "Authorization": auth_header}
        index       = get_azure_index(req.azure.org, req.azure.project, req.azure.team, headers)
        states      = get_or_load_state_cache(req.azure.org, req.azure.project, auth_header)

        tickets = [{"index": i, "key": r.get("key"),
                    "title": ((r.get("fields") or {}).get("summary") or "").strip() or "Untitled"}
                   for i, r in enumerate(raw_issues)]
        yield {"type": "start", "mode": mode, "total": len(raw_issues), "azure_items": index["count"],
               "tickets": tickets,
               "date_filter": {"field": df.field, "from_date": df.from_date, "to_date": df.to_date}}

        ctx = {"req": req, "dry_run": dry_run, "headers": headers, "claimed": set(),
               "by_key": index["by_key"], "by_title": index["by_title"],
               "story_states": states.get("User Story", []), "task_states": states.get("Task", [])}
        counts  = {"create": 0, "update": 0, "skip": 0, "fail": 0}
        results = []

        for i, raw in enumerate(raw_issues):
            ctx["index"] = i
            yield {"type": "ticket_start", "index": i, "key": raw.get("key")}
            result = yield from _process_ticket(raw, ctx)
            result["index"] = i
            counts[result["action"]] = counts.get(result["action"], 0) + 1
            results.append(result)
            print(f"  [SYNC {mode.upper()}] {result['key']}: {result['action']}")
            yield {"type": "ticket_done", **result}

        yield {"type": "done", "mode": mode, "summary": {"total": len(raw_issues), **counts},
               "timestamp": datetime.now(timezone.utc).isoformat()}
    except requests.exceptions.Timeout:
        yield {"type": "error", "error": f"Jira or Azure DevOps didn't respond within {HTTP_TIMEOUT} seconds. Try again."}
    except requests.exceptions.ConnectionError:
        yield {"type": "error", "error": "Couldn't connect to Jira or Azure DevOps. Check the URLs and your network."}
    except Exception as e:
        yield {"type": "error", "error": str(e)}


def run_sync_to_completion(req: SyncRequest, dry_run: bool) -> dict:
    """Non-streaming wrapper: runs the whole sync and returns the final report."""
    report = {"created": [], "updated": [], "skipped": [], "failed": []}
    bucket = {"create": "created", "update": "updated", "skip": "skipped", "fail": "failed"}
    summary = None
    for ev in sync_events(req, dry_run):
        if ev["type"] == "error":
            return {"status": "error", "error": ev["error"]}
        if ev["type"] == "ticket_done":
            report[bucket[ev["action"]]].append(ev)
        if ev["type"] == "done":
            summary = ev["summary"]
    return {"status": "preview" if dry_run else "completed", "is_dry_run": dry_run,
            "summary": summary, "details": report}


# ============================================
# API ENDPOINTS  (used by the UI)
# ============================================

@app.get("/api/default-config")
def api_default_config():
    """Return the built-in default mapping configuration."""
    return default_mapping_config().model_dump()


@app.post("/api/test-connection")
def api_test_connection(req: SyncRequest):
    """Validate the Jira + Azure credentials supplied in the request body."""
    error = _validate_request(req)
    if error:
        return {"jira_ok": False, "azure_ok": False, "errors": [error], "all_ok": False}

    errors = []
    jira_ok, jira_user = False, None
    try:
        r = requests.get(f"{req.jira.base_url}/rest/api/3/myself", auth=(req.jira.email, req.jira.api_token),
                         headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            jira_ok, jira_user = True, r.json().get("displayName", "unknown")
            p = requests.get(f"{req.jira.base_url}/rest/api/3/project/{_q(req.jira.project_key)}",
                             auth=(req.jira.email, req.jira.api_token),
                             headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
            if p.status_code != 200:
                jira_ok = False
                errors.append(f"Jira project '{req.jira.project_key}' wasn't found or you don't have access to it.")
        else:
            errors.append(f"Jira auth failed (HTTP {r.status_code}). Check the email and API token.")
    except Exception as e:
        errors.append(f"Jira connection error: {e}")

    azure_ok, azure_proj = False, None
    headers = {"Authorization": _make_azure_auth_header(req.azure.pat)}
    try:
        r = requests.get(f"https://dev.azure.com/{_q(req.azure.org)}/_apis/projects/{_q(req.azure.project)}?api-version=7.1",
                         headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            azure_ok, azure_proj = True, r.json().get("name", "unknown")
            t = requests.get(f"https://dev.azure.com/{_q(req.azure.org)}/_apis/projects/{_q(req.azure.project)}"
                             f"/teams/{_q(req.azure.team)}?api-version=7.1", headers=headers, timeout=HTTP_TIMEOUT)
            if t.status_code != 200:
                azure_ok = False
                errors.append(f"Azure team '{req.azure.team}' wasn't found in project '{azure_proj}'.")
        else:
            errors.append(f"Azure auth failed (HTTP {r.status_code}). Check the organisation, project and PAT.")
    except Exception as e:
        errors.append(f"Azure connection error: {e}")

    return {"jira_ok": jira_ok, "jira_user": jira_user, "azure_ok": azure_ok, "azure_project": azure_proj,
            "errors": errors, "all_ok": jira_ok and azure_ok}


@app.post("/api/sync/stream")
def api_sync_stream(req: SyncRequest, dry_run: bool = True):
    """Run a sync and stream progress as newline-delimited JSON, one event per line."""
    def body():
        for ev in sync_events(req, dry_run):
            yield json.dumps(ev, default=str) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sync/preview")
def api_sync_preview(req: SyncRequest):
    """Dry-run sync — returns what would be created/updated, without changing anything."""
    return run_sync_to_completion(req, dry_run=True)


@app.post("/api/sync/run")
def api_sync_run(req: SyncRequest):
    """Live sync using the supplied config."""
    return run_sync_to_completion(req, dry_run=False)
