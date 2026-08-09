#!/usr/bin/env python3
"""Run the local OSF project export checklist.

The application uses only the Python standard library. It keeps the OSF Personal
Access Token in the Python process, serves the checklist only on 127.0.0.1,
and preserves the OSF project/component hierarchy in every export.
"""

from __future__ import annotations
from email.utils import parsedate_to_datetime

import argparse
import csv
import errno
import getpass
import html
import json
import os
import queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable


PRODUCTION_API = "https://api.osf.io/v2"
TEST_API = "https://api.test.osf.io/v2"
JSONAPI_MEDIA_TYPE = "application/vnd.api+json"
SCRIPT_VERSION = "0.15.0"
PAGE_SIZE = 10
REQUEST_TIMEOUT = 90
REQUEST_PAUSE_SECONDS = 0.5
MAX_RETRIES = 6
MIN_MAINTENANCE_RETRY_SECONDS = 5 * 60
FILE_ARCHIVE_PAUSE_SECONDS = 2.0
OSF_STATUS_URL = "https://status.cos.io/"
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = f"OSF-Export-Checklist/{SCRIPT_VERSION}"

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
VALID_ACTIONS = {
    "metadata",
    "wiki_current",
    "wiki_history",
    "logs",
    "files",
}

ACTION_FOLDER_PARTS = {
    "metadata": ("Metadata",),
    "wiki_current": ("Wikis", "Current"),
    "wiki_history": ("Wikis", "Version History"),
    "logs": ("Activity Logs",),
    "files": ("Files",),
}

PERMISSION_PRECEDENCE = ("admin", "write", "read")

PROVIDER_LABELS = {
    "osfstorage": "OSF Storage",
    "box": "Box",
    "dataverse": "Dataverse",
    "dropbox": "Dropbox",
    "figshare": "figshare",
    "github": "GitHub",
    "gitlab": "GitLab",
    "googledrive": "Google Drive",
    "mendeley": "Mendeley",
    "onedrive": "OneDrive",
    "owncloud": "ownCloud",
    "s3": "Amazon S3",
    "zotero": "Zotero",
}

# These relationships add useful descriptive or provenance metadata without
# recursively exporting the content of another project.  Files resolves only
# the node's storage-provider records; it does not traverse or download files.
METADATA_RELATIONSHIPS = {
    "contributors": "Contributors",
    "bibliographic_contributors": "Bibliographic Contributors",
    "implicit_contributors": "Implicit Contributors",
    "affiliated_institutions": "Affiliated Institutions",
    "identifiers": "Identifiers",
    "license": "License",
    "citation": "Citation",
    "region": "Storage Region",
    "registrations": "Registrations",
    "draft_registrations": "Draft Registrations",
    "linked_nodes": "Linked Projects",
    "linked_registrations": "Linked Registrations",
    "linked_by_nodes": "Projects Linking Here",
    "linked_by_registrations": "Registrations Linking Here",
    "preprints": "Preprints",
    "forks": "Forks",
    "forked_from": "Forked From",
    "template_node": "Template Project",
    "files": "Storage Providers",
}

RELATIONSHIP_NOT_EXPANDED = {
    "wikis": "Exported by the separate wiki action.",
    "logs": "Exported by the separate activity-log action.",
    "comments": "Comments are outside this metadata export.",
    "view_only_links": "Not expanded because view-only links can grant access to private content.",
}


class ChecklistError(RuntimeError):
    """A failure that can be shown directly to the user."""


class JobCancelled(ChecklistError):
    """Raised when a queued or running export is cancelled."""


@dataclass
class ExportIssue:
    severity: str
    project_guid: str
    project_title: str
    element_type: str
    element_id: str
    reason: str
    detail: str

    def public_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class NodeRecord:
    guid: str
    title: str
    raw: dict[str, Any]
    public: bool = False
    category: str = ""
    date_modified: str = ""
    url: str = ""
    parent_guid: str | None = None
    permission: str = ""
    children: list["NodeRecord"] = field(default_factory=list)
    folder: Path | None = None
    metadata_count: int = 0
    related_metadata_count: int = 0
    wiki_count: int = 0
    wiki_version_count: int = 0
    log_count: int = 0
    file_archive_count: int = 0
    file_archive_bytes: int = 0

    @property
    def display_name(self) -> str:
        return title_guid_name(self.title, self.guid)

    @property
    def visibility(self) -> str:
        return "Public" if self.public else "Private"


@dataclass
class ExportJob:
    id: str
    root_guid: str
    root_title: str
    action: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "Waiting to start"
    issues: list[ExportIssue] = field(default_factory=list)
    output_folder: str = ""
    created_at: str = field(default_factory=lambda: utc_now())
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False
    retry_of: str = ""
    retry_mode: str = ""
    retry_scopes: dict[str, list[str]] = field(default_factory=dict)
    retry_note: str = ""

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("cancel_requested", None)
        result["issue_groups"] = group_export_issues(self.issues)
        result["omission_count"] = sum(
            1 for issue in self.issues if issue.severity == "omission"
        )
        result["critical_failure_count"] = sum(
            1 for issue in self.issues if issue.severity == "critical"
        )
        result["affected_project_count"] = len(
            {issue.project_guid for issue in self.issues if issue.project_guid}
        )
        result["retry_plan"] = build_retry_plan(
            self.action,
            self.issues,
            self.status,
        )
        return result


def concise_issue_reason(error: Exception | str) -> str:
    text = re.sub(r"\s+", " ", str(error)).strip()
    lower = text.casefold()

    if "osf is undergoing maintenance" in lower:
        return (
            "OSF is undergoing maintenance; try again in a few minutes"
        )

    status = re.search(r"HTTP\s+(\d{3})", text, re.I)
    if status:
        code = status.group(1)
        labels = {
            "401": "OSF rejected the token (HTTP 401)",
            "403": "OSF denied access (HTTP 403)",
            "404": "OSF could not find the resource (HTTP 404)",
            "405": "the OSF endpoint did not support the request (HTTP 405)",
            "429": "OSF rate limiting continued after automatic retries (HTTP 429)",
            "500": "OSF continued returning a server error after automatic retries (HTTP 500)",
            "502": "OSF continued returning a bad gateway error after automatic retries (HTTP 502)",
            "503": "OSF continued returning an unavailable response after automatic retries (HTTP 503)",
            "504": "OSF continued returning a gateway timeout after automatic retries (HTTP 504)",
        }
        return labels.get(code, f"OSF returned HTTP {code}")
    if "could not reach osf after several attempts" in lower:
        return "OSF could not be reached after automatic retries"
    if "no current-content download link" in lower:
        return "OSF did not provide a current wiki download link"
    if "no version-content download link" in lower:
        return "OSF did not provide a wiki-version download link"
    if "no version-history link" in lower:
        return "OSF did not provide a wiki version-history link"
    if isinstance(error, OSError):
        if (
            error.errno == errno.ENAMETOOLONG
            or getattr(error, "winerror", None) == 206
        ):
            return (
                "the local export path exceeded the operating system's length "
                "limit; retry using a shorter --output location"
            )
        return "the local output file could not be written"
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return first_sentence[:240] or "an unknown error occurred"


def add_issue(
    issues: list[ExportIssue],
    node: NodeRecord,
    element_type: str,
    element_id: str,
    error: Exception | str,
    *,
    critical: bool = False,
) -> None:
    issues.append(
        ExportIssue(
            severity="critical" if critical else "omission",
            project_guid=node.guid,
            project_title=node.title,
            element_type=element_type,
            element_id=str(element_id or ""),
            reason=concise_issue_reason(error),
            detail=str(error),
        )
    )


def plural_element(value: str, count: int) -> str:
    if count == 1:
        return value
    if value.endswith("history index"):
        return value.replace("history index", "history indexes")
    if value.endswith("metadata"):
        return value
    if value.endswith("y") and not value.endswith(("ay", "ey", "oy")):
        return f"{value[:-1]}ies"
    return f"{value}s"


def group_export_issues(issues: list[ExportIssue]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[ExportIssue]] = {}
    for issue in issues:
        grouped.setdefault(
            (issue.severity, issue.element_type, issue.reason),
            [],
        ).append(issue)
    output: list[dict[str, Any]] = []
    for (severity, element_type, reason), members in grouped.items():
        projects = {(issue.project_guid, issue.project_title) for issue in members}
        element_count = len(members)
        project_count = len(projects)
        if element_count == 1:
            member = members[0]
            element_label = f"1 {element_type}"
            if member.element_id and member.element_id != member.project_guid:
                element_label += f" [{member.element_id}]"
            message = (
                f"{element_label} for {member.project_title} [{member.project_guid}] "
                f"could not be exported because {reason}."
            )
        else:
            message = (
                f"{element_count} {plural_element(element_type, element_count)} across "
                f"{project_count} {'project/component' if project_count == 1 else 'projects/components'} "
                f"could not be exported because {reason}."
            )
        output.append(
            {
                "severity": severity,
                "element_type": element_type,
                "element_count": element_count,
                "project_count": project_count,
                "reason": reason,
                "message": message,
                "projects": [
                    {"guid": guid, "title": title}
                    for guid, title in sorted(
                        projects, key=lambda item: (item[1].casefold(), item[0])
                    )
                ],
                "element_ids": [
                    issue.element_id for issue in members if issue.element_id
                ],
                "elements": [
                    {
                        "project_guid": issue.project_guid,
                        "project_title": issue.project_title,
                        "element_type": issue.element_type,
                        "element_id": issue.element_id,
                        "description": (
                            f"{issue.project_title} [{issue.project_guid}] — "
                            f"{issue.element_type}"
                            + (
                                f" [{issue.element_id}]"
                                if issue.element_id
                                and issue.element_id != issue.project_guid
                                else ""
                            )
                        ),
                    }
                    for issue in members
                ],
            }
        )
    return output


def action_content_types(action: str) -> list[str]:
    return [action]


def issue_content_type(issue: ExportIssue, action: str) -> str | None:
    element_type = issue.element_type.casefold()
    if element_type == "core metadata or primary requested content":
        return None
    if "wiki" in element_type:
        if "version" in element_type or "history" in element_type:
            return "wiki_history"
        if element_type == "wiki collection" and action in {
            "wiki_current",
            "wiki_history",
        }:
            return action
        return "wiki_current"
    if "activity-log" in element_type:
        return "logs"
    if "file" in element_type or "storage provider" in element_type:
        return "files"
    if "metadata" in element_type:
        return "metadata"
    return None


def build_retry_plan(
    action: str,
    issues: list[ExportIssue],
    status: str,
) -> dict[str, Any]:
    if status == "cancelled":
        return {
            "mode": "full",
            "scopes": {},
            "description": "Retry the full action because a cancellation does not identify every unfinished element.",
        }
    if not issues:
        return {
            "mode": "full",
            "scopes": {},
            "description": "Retry the full action.",
        }
    allowed = set(action_content_types(action))
    scopes: dict[str, set[str]] = {}
    for issue in issues:
        content_type = issue_content_type(issue, action)
        if not content_type or content_type not in allowed or not issue.project_guid:
            return {
                "mode": "full",
                "scopes": {},
                "description": (
                    "Retry the full action because at least one failure cannot be "
                    "safely isolated."
                ),
            }
        scopes.setdefault(content_type, set()).add(issue.project_guid)
    public_scopes = {
        content_type: sorted(guids) for content_type, guids in sorted(scopes.items())
    }
    scope_parts = [
        f"{content_type} for {len(guids)} "
        f"{'project/component' if len(guids) == 1 else 'projects/components'}"
        for content_type, guids in public_scopes.items()
    ]
    return {
        "mode": "targeted",
        "scopes": public_scopes,
        "description": "Retry only " + "; ".join(scope_parts) + ".",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_title(value: Any, fallback: str) -> str:
    title = html.unescape(str(value or "")).strip()
    return title or fallback


def effective_permission(values: Any) -> str:
    """Return the requesting user's highest effective OSF permission."""
    if isinstance(values, str):
        permissions = {values.casefold()}
    elif isinstance(values, list):
        permissions = {str(value).casefold() for value in values}
    else:
        return ""
    for permission in PERMISSION_PRECEDENCE:
        if permission in permissions:
            return permission
    return sorted(permissions)[0] if permissions else ""


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider.casefold(), provider.replace("_", " ").title())


def zip_download_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "zip" for key, _ in query):
        query.append(("zip", ""))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate text without splitting a UTF-8 character."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def safe_segment(value: str, max_length: int = 140) -> str:
    if max_length < 1:
        raise ValueError("max_length must be at least 1")

    value = unicodedata.normalize("NFC", str(value))
    value = INVALID_FILENAME_CHARS.sub(" - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")

    if not value:
        value = "Untitled"

    value = value[:max_length].rstrip(" .")
    value = truncate_utf8(value, max_length)

    # Windows reserves device names even when they have extensions,
    # including CON.txt, NUL.json, COM1.csv, and LPT1.zip.
    windows_base_name = value.partition(".")[0].rstrip(" ").upper()
    if windows_base_name in WINDOWS_RESERVED_NAMES:
        value = truncate_utf8(f"_{value}", max_length)

    return value or "Untitled"


def title_guid_name(title: str, guid: str, max_length: int = 140) -> str:
    """Combine a shortened title with its GUID within a byte limit."""
    suffix = f" [{guid}]"
    suffix_bytes = len(suffix.encode("utf-8"))
    title_bytes = max_length - suffix_bytes

    if title_bytes < 1:
        raise ValueError("max_length is too small to retain the GUID")

    safe_title = safe_segment(title, max_length=title_bytes)
    return f"{safe_title}{suffix}"


def related_href(relationship: dict[str, Any] | None) -> str | None:
    related = ((relationship or {}).get("links") or {}).get("related")
    if isinstance(related, dict):
        related = related.get("href")
    return related if isinstance(related, str) and related else None


def link_href(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("href")
    return value if isinstance(value, str) and value else None


def relationship_id(relationship: dict[str, Any] | None) -> str | None:
    data = (relationship or {}).get("data")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"]).lower()
    href = related_href(relationship)
    if not href:
        return None
    segments = [part for part in urllib.parse.urlparse(href).path.split("/") if part]
    for marker in ("nodes", "registrations", "users"):
        if marker in segments:
            index = segments.index(marker) + 1
            if index < len(segments):
                return segments[index].lower()
    return None


def add_page_size(
    url: str,
    size: int = PAGE_SIZE,
    default_sort: str | None = None,
) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)

    if not any(key == "page[size]" for key, _ in query):
        query.append(("page[size]", str(size)))

    if (
        default_sort is not None
        and not any(key == "sort" for key, _ in query)
    ):
        query.append(("sort", default_sort))

    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query))
    )

def extract_guid(value: str) -> str:
    value = value.strip()
    direct = re.fullmatch(r"([A-Za-z0-9]{5})(?:_v\d+)?", value)
    if direct:
        return direct.group(1).lower()
    parsed = urllib.parse.urlparse(value if "://" in value else f"https://{value}")
    for segment in [
        urllib.parse.unquote(part) for part in parsed.path.split("/") if part
    ]:
        match = re.fullmatch(r"([A-Za-z0-9]{5})(?:_v\d+)?", segment)
        if match:
            return match.group(1).lower()
    raise ChecklistError(f"Could not find a five-character OSF GUID in: {value}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the bearer token before urllib follows an untrusted redirect."""

    def __init__(self, trusted_hosts: set[str]) -> None:
        super().__init__()
        self.trusted_hosts = trusted_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected
            and urllib.parse.urlparse(newurl).hostname not in self.trusted_hosts
        ):
            redirected.remove_header("Authorization")
        return redirected


class OSFClient:
    def __init__(
        self, api_base: str, token: str, timeout: int = REQUEST_TIMEOUT
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.cancel_requested: Callable[[], bool] | None = None
        self._request_lock = threading.Lock()
        self._has_started_request = False
        api_host = urllib.parse.urlparse(self.api_base).hostname or ""
        self.trusted_hosts = {api_host}
        self.opener = urllib.request.build_opener(
            SafeRedirectHandler(self.trusted_hosts)
        )

    def api_url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def wait(self, seconds: float) -> None:
        """Wait between attempts while still responding to job cancellation."""
        remaining = max(0.0, seconds)
        while remaining > 0:
            if self.cancel_requested and self.cancel_requested():
                raise JobCancelled("Cancelled by user")
            interval = min(1.0, remaining)
            time.sleep(interval)
            remaining -= interval

    def headers(
        self, url: str, accept: str = JSONAPI_MEDIA_TYPE,
    ) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if urllib.parse.urlparse(url).hostname in self.trusted_hosts:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def trust_download_host(self, url: str) -> None:
        """Trust only OSF file-service hosts returned by the authenticated API."""
        host = (urllib.parse.urlparse(url).hostname or "").casefold()
        api_host = (urllib.parse.urlparse(self.api_base).hostname or "").casefold()
        if (
            host == api_host
            or host == "files.osf.io"
            or (host.startswith("files.") and host.endswith(".osf.io"))
        ):
            self.trusted_hosts.add(host)
            return
        raise ChecklistError(
            f"OSF returned an unexpected file-download host: {host or 'missing host'}"
        )

    def open(
        self,
        url: str,
        accept: str = JSONAPI_MEDIA_TYPE,
        extra: dict[str, str] | None = None,
    ):
        headers = self.headers(url, accept)
        headers.update(extra or {})
        request = urllib.request.Request(url, headers=headers, method="GET")

        with self._request_lock:
            if self._has_started_request:
                self.wait(REQUEST_PAUSE_SECONDS)
            self._has_started_request = True
            return self.opener.open(request, timeout=self.timeout)

    def request_bytes(
        self,
        url: str,
        accept: str = JSONAPI_MEDIA_TYPE,
        absent_statuses: set[int] | None = None,
    ) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with self.open(url, accept) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if absent_statuses and error.code in absent_statuses:
                    return None
                last_error = error
                if error.code in RETRYABLE_HTTP_STATUSES and attempt < MAX_RETRIES - 1:
                    delay = retry_delay(error, attempt)
                    self.wait(delay)
                    continue
                raise http_error(error) from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                permanent = connection_error(error)
                if permanent is not None:
                    raise permanent from error
                if attempt < MAX_RETRIES - 1:
                    self.wait(min(30.0, 2.0**attempt))
                    continue
                raise ChecklistError(
                    "Could not reach OSF after several attempts. Check the internet "
                    "connection, VPN, proxy, firewall, or institutional network."
                ) from error
        raise ChecklistError(f"OSF request failed: {last_error}")

    def download_file(
        self,
        url: str,
        destination: Path,
        cancelled: Callable[[], bool] | None = None,
    ) -> int:
        """Stream an OSF ZIP response to disk and return its byte size."""
        self.trust_download_host(url)
        temporary = destination.with_name(destination.name + ".part")
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with self.open(
                    url, "application/zip, application/octet-stream;q=0.9"
                ) as response:
                    with temporary.open("wb") as handle:
                        while True:
                            if cancelled and cancelled():
                                raise JobCancelled("Cancelled by user")
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                if not zipfile.is_zipfile(temporary):
                    raise ChecklistError(
                        "OSF returned a response that was not a valid ZIP archive."
                    )
                os.replace(temporary, destination)
                return destination.stat().st_size
            except JobCancelled:
                temporary.unlink(missing_ok=True)
                raise
            except urllib.error.HTTPError as error:
                last_error = error
                temporary.unlink(missing_ok=True)
                if error.code in RETRYABLE_HTTP_STATUSES and attempt < MAX_RETRIES - 1:
                    self.wait(retry_delay(error, attempt))
                    continue
                raise http_error(error) from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                temporary.unlink(missing_ok=True)
                permanent = connection_error(error)
                if permanent is not None:
                    raise permanent from error
                if attempt < MAX_RETRIES - 1:
                    self.wait(min(30.0, 2.0**attempt))
                    continue
                raise ChecklistError(
                    "Could not reach the OSF file service after several attempts."
                ) from error
            except ChecklistError:
                temporary.unlink(missing_ok=True)
                raise
            except OSError:
                temporary.unlink(missing_ok=True)
                raise
        raise ChecklistError(f"OSF file download failed: {last_error}")

    def get_json(self, url: str) -> dict[str, Any]:
        raw = self.request_bytes(url)
        if raw is None:
            raise ChecklistError(f"OSF returned no response for {url}")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChecklistError(
                f"OSF returned an unexpected response for {url}"
            ) from error
        if not isinstance(result, dict):
            raise ChecklistError(f"OSF returned an unexpected response for {url}")
        return result

    def get_optional_json(
        self,
        url: str,
        absent_statuses: set[int] | None = None,
    ) -> dict[str, Any] | None:
        raw = self.request_bytes(url, absent_statuses=absent_statuses or {404})
        if raw is None:
            return None
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChecklistError(
                f"OSF returned an unexpected response for {url}"
            ) from error
        if not isinstance(result, dict):
            raise ChecklistError(f"OSF returned an unexpected response for {url}")
        return result

    def get_text(self, url: str) -> str:
        raw = self.request_bytes(url, "text/markdown, text/plain;q=0.9, */*;q=0.1")
        if raw is None:
            raise ChecklistError(f"OSF returned no response for {url}")
        return raw.decode("utf-8", errors="replace")

    def paginate(
        self,
        url: str,
        *,
        default_sort: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        next_url: str | None = add_page_size(
            url,
            default_sort=default_sort,
        )
        seen: set[str] = set()

        while next_url:
            if next_url in seen:
                raise ChecklistError(f"OSF repeated a pagination link: {next_url}")

            seen.add(next_url)
            document = self.get_json(next_url)
            data = document.get("data", [])

            if isinstance(data, dict):
                data = [data]

            if not isinstance(data, list):
                raise ChecklistError(
                    f"Unexpected paginated response for {next_url}"
                )

            for item in data:
                if isinstance(item, dict):
                    yield item

            next_value = (document.get("links") or {}).get("next")
            if isinstance(next_value, dict):
                next_value = next_value.get("href")

            next_url = (
                add_page_size(
                    next_value,
                    default_sort=default_sort,
                )
                if isinstance(next_value, str) and next_value
                else None
            )

def http_error_body(error: urllib.error.HTTPError) -> bytes:
    """Read an HTTP error body once and retain it for later classification."""
    cached = getattr(error, "_osf_response_body", None)
    if isinstance(cached, bytes):
        return cached

    try:
        body = error.read()
    except Exception:
        body = b""

    setattr(error, "_osf_response_body", body)
    return body


def osf_maintenance_mode(error: urllib.error.HTTPError) -> bool:
    """Return whether OSF identified this 503 as maintenance mode."""
    if error.code != 503:
        return False

    body = http_error_body(error)
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    if not isinstance(document, dict):
        return False

    meta = document.get("meta")
    return (
        isinstance(meta, dict)
        and meta.get("maintenance_mode") is True
    )


def retry_after_delay(
    error: urllib.error.HTTPError,
) -> float | None:
    """Return OSF's requested retry delay, if it supplied a valid value."""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if not retry_after:
        return None

    # Retry-After may specify a number of seconds.
    try:
        delay = float(retry_after)
        if 0 <= delay < float("inf"):
            return delay
    except ValueError:
        pass

    # Retry-After may instead specify an HTTP date.
    try:
        retry_at = parsedate_to_datetime(retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)

        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    instructed_delay = retry_after_delay(error)

    if osf_maintenance_mode(error):
        return max(
            float(MIN_MAINTENANCE_RETRY_SECONDS),
            instructed_delay if instructed_delay is not None else 0.0,
        )

    if instructed_delay is not None:
        return instructed_delay

    return 2.0**attempt

def connection_error(error: BaseException) -> ChecklistError | None:
    """Classify permanent local TLS/DNS failures; return None when retrying may help."""
    reason: Any = error.reason if isinstance(error, urllib.error.URLError) else error
    text = str(reason)
    if (
        isinstance(reason, ssl.SSLCertVerificationError)
        or "CERTIFICATE_VERIFY_FAILED" in text
    ):
        return ChecklistError(
            "Python could not verify OSF's SSL certificate. On macOS with the "
            "python.org installer, open Applications > Python 3.x and run "
            "Install Certificates.command. A VPN, proxy, or security product may "
            "also require an organizational certificate to be added to Python."
        )
    if isinstance(reason, ssl.SSLError):
        return ChecklistError(
            "Python could not establish a secure SSL connection to OSF. Check the "
            "computer clock, VPN, proxy, firewall, and Python certificate installation."
        )
    if isinstance(reason, socket.gaierror):
        return ChecklistError(
            "The computer could not resolve the OSF address. Check DNS, the internet "
            "connection, VPN, proxy, or institutional network."
        )
    return None


def http_error(error: urllib.error.HTTPError) -> ChecklistError:
    body_bytes = http_error_body(error)
    body = body_bytes.decode("utf-8", errors="replace")[:800]

    if osf_maintenance_mode(error):
        return ChecklistError(
            "OSF is undergoing maintenance. Try again in a few minutes. "
            f"Check the OSF status page: {OSF_STATUS_URL}"
        )

    if error.code == 401:
        message = (
            "OSF rejected the personal access token (HTTP 401). Copy a current token "
            "from the matching OSF environment with osf.full_read permission. If the "
            "OSF_TOKEN environment variable is set, clear or update it before trying again."
        )
    elif error.code == 403:
        message = (
            "OSF denied access (HTTP 403). Confirm that the token can view "
            "this content."
        )
    elif error.code == 404:
        message = "OSF could not find this project or resource (HTTP 404)."
    else:
        message = f"OSF request failed with HTTP {error.code}."

    if body:
        message += f" Response: {body}"

    return ChecklistError(message)

def node_from_api(record: dict[str, Any], parent_guid: str | None = None) -> NodeRecord:
    guid = str(record.get("id") or "").strip().lower()
    if not guid:
        raise ChecklistError(
            "An OSF project/component response did not include a GUID."
        )
    attributes = record.get("attributes") or {}
    relationships = record.get("relationships") or {}
    links = record.get("links") or {}
    return NodeRecord(
        guid=guid,
        title=clean_title(attributes.get("title"), f"Untitled OSF item {guid}"),
        raw=record,
        public=bool(attributes.get("public")),
        category=str(attributes.get("category") or ""),
        date_modified=str(attributes.get("date_modified") or ""),
        url=str(links.get("html") or f"https://osf.io/{guid}/"),
        parent_guid=parent_guid
        if parent_guid is not None
        else relationship_id(relationships.get("parent")),
        permission=effective_permission(attributes.get("current_user_permissions")),
    )


def get_account(client: OSFClient) -> tuple[str, str]:
    data = client.get_json(client.api_url("users/me/")).get("data") or {}
    attributes = data.get("attributes") or {}
    name = (
        attributes.get("full_name")
        or attributes.get("given_name")
        or data.get("id")
        or "OSF user"
    )
    return str(name), str(data.get("id") or "unknown")


def get_inventory(client: OSFClient) -> list[dict[str, Any]]:
    url = client.api_url(
        f"users/me/nodes/?{urllib.parse.urlencode({'page[size]': PAGE_SIZE})}"
    )
    records: list[dict[str, Any]] = []

    for record in client.paginate(
        url,
        default_sort="-date_modified",
    ):
        records.append(record)
        if len(records) % 100 == 0:
            print(
                f"Retrieved {len(records):,} projects/components...",
                flush=True,
            )

    return records


def build_hierarchy(
    records: list[dict[str, Any]],
    forced_root_guids: set[str] | None = None,
) -> tuple[list[NodeRecord], list[NodeRecord]]:
    forced_root_guids = {
        guid.casefold() for guid in (forced_root_guids or set())
    }

    nodes: dict[str, NodeRecord] = {}
    for record in records:
        node = node_from_api(record)

        if node.guid in forced_root_guids:
            node.parent_guid = None

        nodes[node.guid] = node
    roots: list[NodeRecord] = []
    orphans: list[NodeRecord] = []
    for node in nodes.values():
        if node.parent_guid and node.parent_guid in nodes:
            nodes[node.parent_guid].children.append(node)
        elif node.parent_guid:
            orphans.append(node)
        else:
            roots.append(node)

    def sort_branch(node: NodeRecord) -> None:
        node.children.sort(key=lambda item: (item.title.casefold(), item.guid))
        for child in node.children:
            sort_branch(child)

    for node in roots + orphans:
        sort_branch(node)
    roots.sort(
        key=lambda item: (item.date_modified, item.title.casefold()), reverse=True
    )
    orphans.sort(
        key=lambda item: (item.date_modified, item.title.casefold()), reverse=True
    )
    return roots, orphans


def discover_tree(client: OSFClient, root_guid: str) -> NodeRecord:
    """Retrieve a complete OSF subtree and reconstruct its hierarchy locally."""
    root_guid = root_guid.casefold()

    query = urllib.parse.urlencode(
        {
            "filter[root]": root_guid,
            "page[size]": PAGE_SIZE,
        }
    )
    tree_url = client.api_url(f"nodes/?{query}")
    records = list(client.paginate(tree_url))

    seen_guids: set[str] = set()
    for record in records:
        guid = str(record.get("id") or "").strip().casefold()
        if not guid:
            raise ChecklistError(
                "An OSF hierarchy response did not include a node GUID."
            )
        if guid in seen_guids:
            raise ChecklistError(
                f"OSF repeated node GUID {guid} while retrieving the hierarchy."
            )
        seen_guids.add(guid)

    # The filtered endpoint normally includes the requested root. Retain a
    # fallback so a change in API behavior does not make the export unusable.
    if root_guid not in seen_guids:
        root_data = client.get_json(
            client.api_url(f"nodes/{root_guid}/")
        ).get("data")

        if not isinstance(root_data, dict):
            raise ChecklistError(
                "OSF returned an unexpected project response."
            )

        records.insert(0, root_data)
        seen_guids.add(root_guid)

    roots, orphans = build_hierarchy(
        records,
        forced_root_guids={root_guid},
    )

    root = next(
        (node for node in roots if node.guid == root_guid),
        None,
    )
    if root is None:
        raise ChecklistError(
            f"OSF did not return the requested hierarchy root {root_guid}."
        )

    unexpected_roots = [
        node for node in roots if node.guid != root_guid
    ]
    unattached = [*orphans, *unexpected_roots]
    if unattached:
        unattached_guids = sorted(node.guid for node in unattached)
        displayed_guids = ", ".join(unattached_guids[:10])
        if len(unattached_guids) > 10:
            displayed_guids += f", and {len(unattached_guids) - 10} more"

        raise ChecklistError(
            "OSF returned projects/components that could not be connected "
            f"to the requested hierarchy: {displayed_guids}"
        )

    connected_guids = {
        node.guid for node in flatten_tree(root)
    }
    disconnected_guids = sorted(seen_guids - connected_guids)
    if disconnected_guids:
        displayed_guids = ", ".join(disconnected_guids[:10])
        if len(disconnected_guids) > 10:
            displayed_guids += (
                f", and {len(disconnected_guids) - 10} more"
            )

        raise ChecklistError(
            "OSF returned a disconnected or cyclic project hierarchy "
            f"involving: {displayed_guids}"
        )

    return root


def flatten_tree(root: NodeRecord) -> list[NodeRecord]:
    output: list[NodeRecord] = []

    def walk(node: NodeRecord) -> None:
        output.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return output


def ensure_hierarchy(root: NodeRecord, root_folder: Path, action: str) -> None:
    def create(node: NodeRecord, folder: Path) -> None:
        node.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        action_output_folder(node, action).mkdir(parents=True, exist_ok=True)
        for child in node.children:
            create(child, folder / child.display_name)

    create(root, root_folder)


def action_output_folder(node: NodeRecord, action: str) -> Path:
    assert node.folder is not None
    folder = node.folder
    for part in ACTION_FOLDER_PARTS[action]:
        folder /= part
    return folder


def owner_prefixed_name(
    node: NodeRecord,
    suffix: str,
    max_length: int = 220,
) -> str:
    """Create a filename that retains the owner GUID within a byte limit."""
    suffix_bytes = len(suffix.encode("utf-8"))
    guid_bytes = len(f" [{node.guid}]".encode("utf-8"))
    minimum_title_bytes = 1

    if suffix_bytes + guid_bytes + minimum_title_bytes > max_length:
        raise ChecklistError(
            "A generated filename suffix exceeded the portable filename limit."
        )

    owner_bytes = max_length - suffix_bytes
    return f"{title_guid_name(node.title, node.guid, owner_bytes)}{suffix}"


def check_cancel(job: ExportJob) -> None:
    if job.cancel_requested:
        raise JobCancelled("Cancelled by user")


ProgressCallback = Callable[[float, str], None]


def write_json_document(path: Path, document: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def collect_paginated_document(
    client: OSFClient,
    url: str,
    first_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect every page while retaining source and pagination provenance."""
    next_url: str | None = add_page_size(url)
    document = first_document
    records: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    while next_url:
        if next_url in seen:
            raise ChecklistError(f"OSF repeated a pagination link: {next_url}")
        seen.add(next_url)
        current = document if document is not None else client.get_json(next_url)
        document = None
        data = current.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if data is None:
            data = []
        if not isinstance(data, list):
            raise ChecklistError(f"Unexpected metadata response for {next_url}")
        records.extend(item for item in data if isinstance(item, dict))
        pages.append(
            {
                "url": next_url,
                "record_count": sum(1 for item in data if isinstance(item, dict)),
                "meta": current.get("meta") or {},
            }
        )
        next_value = (current.get("links") or {}).get("next")
        if isinstance(next_value, dict):
            next_value = next_value.get("href")
        next_url = next_value if isinstance(next_value, str) and next_value else None
    return {
        "status": "complete",
        "source_url": url,
        "exported_utc": utc_now(),
        "record_count": len(records),
        "pages": pages,
        "data": records,
    }


def relationship_linkage(relationship: dict[str, Any]) -> Any:
    return relationship.get("data")


def metadata_record_label(record: dict[str, Any]) -> str:
    attributes = record.get("attributes") or {}
    for key in ("full_name", "title", "name", "value", "category"):
        if attributes.get(key):
            return str(attributes[key])
    embedded_user = ((record.get("embeds") or {}).get("users") or {}).get("data") or {}
    if isinstance(embedded_user, dict):
        user_attributes = embedded_user.get("attributes") or {}
        if user_attributes.get("full_name"):
            return str(user_attributes["full_name"])
    return str(record.get("id") or record.get("type") or "record")


def humanize_metadata_key(value: Any) -> str:
    return str(value).replace("_", " ").strip().title()


def metadata_html_value(value: Any, depth: int = 0) -> str:
    if value is None or value == "":
        return '<span class="empty-value">Not provided</span>'
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        if not value:
            return '<span class="empty-value">None</span>'
        if depth >= 5:
            return f"<pre>{html.escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"
        rows = "".join(
            f"<tr><th>{html.escape(humanize_metadata_key(key))}</th>"
            f"<td>{metadata_html_value(item, depth + 1)}</td></tr>"
            for key, item in value.items()
        )
        return f'<table class="metadata-table nested"><tbody>{rows}</tbody></table>'
    if isinstance(value, list):
        if not value:
            return '<span class="empty-value">None</span>'
        items = "".join(
            f"<li>{metadata_html_value(item, depth + 1)}</li>" for item in value
        )
        return f'<ol class="metadata-list">{items}</ol>'
    text = str(value)
    escaped = html.escape(text)
    if re.fullmatch(r"https?://[^\s]+", text):
        return f'<a href="{html.escape(text, quote=True)}">{escaped}</a>'
    return f'<span class="text-value">{escaped}</span>'


def metadata_records_html(source: dict[str, Any]) -> str:
    status = str(source.get("status") or "unknown")
    if status == "not_found":
        return '<p class="empty-value">No record was found.</p>'
    if status == "not_available":
        return '<p class="empty-value">This endpoint was not available.</p>'
    if status == "error":
        return f'<p class="warning">Could not retrieve this source: {html.escape(str(source.get("error") or "Unknown error"))}</p>'
    records = source.get("data") or []
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list) or not records:
        return '<p class="empty-value">No records.</p>'
    output: list[str] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            output.append(metadata_html_value(record))
            continue
        label = metadata_record_label(record)
        output.append(
            '<details class="record" open>'
            f"<summary>{html.escape(label)} <small>#{index}</small></summary>"
            f"{metadata_html_value(record)}"
            "</details>"
        )
    return "".join(output)

METADATA_CSS = r"""
:root {
  --ink: #243746;
  --muted: #657985;
  --line: #d8e2e7;
  --blue: #1f608d;
  --bg: #f4f7f9;
  --paper: #fff;
  --warn: #8a4b08;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.48 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
}
header {
  padding: 28px max(22px, calc((100% - 1040px) / 2));
  color: #fff;
  background: linear-gradient(135deg, #243746, #2c6e9f);
}
header h1 {
  margin: 0 0 5px;
  font-size: 1.8rem;
}
header p {
  margin: 0;
  opacity: 0.88;
}
main {
  width: min(1040px, calc(100% - 30px));
  margin: 20px auto 60px;
}
section {
  margin: 0 0 16px;
  padding: 18px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 9px;
  box-shadow: 0 3px 12px rgba(20, 45, 60, 0.05);
}
h2 {
  margin: 0 0 11px;
  font-size: 1.18rem;
}
a {
  color: var(--blue);
  overflow-wrap: anywhere;
}
.metadata-table {
  width: 100%;
  border-collapse: collapse;
}
.metadata-table th,
.metadata-table td {
  padding: 7px 9px;
  border: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
.metadata-table th {
  width: 25%;
  background: #f5f8fa;
}
.metadata-table.nested th {
  width: 28%;
  font-size: 0.84rem;
}
.metadata-list {
  margin: 0;
  padding-left: 21px;
}
.metadata-list > li {
  margin: 4px 0;
}
.record {
  margin: 8px 0;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.record summary {
  padding: 8px 10px;
  cursor: pointer;
  font-weight: 700;
  background: #f6f9fa;
}
.record > .metadata-table {
  width: calc(100% - 16px);
  margin: 8px;
}
.empty-value,
.source,
footer {
  color: var(--muted);
}
.source {
  font-size: 0.78rem;
}
.warning {
  color: var(--warn);
}
.text-value,
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
pre {
  font-size: 0.78rem;
}
footer {
  text-align: center;
  font-size: 0.78rem;
}
@media (max-width: 650px) {
  .metadata-table th {
    width: 35%;
  }
}
""".strip()


def render_complete_metadata_html(package: dict[str, Any]) -> str:
    project = package.get("project") or {}
    title = str(project.get("title") or "Untitled OSF project")
    guid = str(project.get("guid") or "unknown")
    core_document = package.get("core_api_response") or {}
    core = core_document.get("data") or {}
    attributes = core.get("attributes") or {} if isinstance(core, dict) else {}
    links = core.get("links") or {} if isinstance(core, dict) else {}
    custom = package.get("custom_item_metadata") or {"status": "not_found"}
    custom_display = (
        custom.get("data") if custom.get("status") == "complete" else custom
    )
    cedar = package.get("cedar_metadata_records") or {"status": "not_available"}
    resolved = package.get("resolved_relationships") or {}
    relationship_sections = "".join(
        f"<section><h2>{html.escape(METADATA_RELATIONSHIPS.get(key, humanize_metadata_key(key)))}</h2>"
        f'<p class="source">Source: {metadata_html_value(source.get("source_url"))}</p>'
        f"{metadata_records_html(source)}</section>"
        for key, source in resolved.items()
        if isinstance(source, dict)
    )
    catalog = package.get("relationship_catalog") or {}
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<title>{html.escape(title)} [{html.escape(guid)}] - Complete Metadata</title>
<style>
{METADATA_CSS}
</style>
</head><body>
<header><h1>{html.escape(title)} [{html.escape(guid)}]</h1><p>Complete OSF metadata export · {html.escape(str(package.get("exported_utc") or ""))}</p></header>
<main>
<section><h2>Project or component</h2>{metadata_html_value(project)}</section>
<section><h2>Core OSF attributes</h2>{metadata_html_value(attributes)}</section>
<section><h2>Core links</h2>{metadata_html_value(links)}</section>
<section><h2>Project hierarchy</h2>{metadata_html_value(package.get("hierarchy") or {})}</section>
<section><h2>Custom metadata</h2>{metadata_html_value(custom_display)}</section>
<section><h2>CEDAR metadata records</h2>{metadata_records_html(cedar)}</section>
{relationship_sections}
<section><h2>Relationship catalog</h2><p class="source">Every relationship from the core response is listed here, including relationships handled by other export actions or deliberately not expanded.</p>{metadata_html_value(catalog)}</section>
<section><h2>Export notes</h2>{metadata_html_value(package.get("notes") or [])}</section>
<footer>Generated locally by OSF Export Checklist {html.escape(SCRIPT_VERSION)}. The complete JSON is stored beside this document.</footer>
</main></body></html>"""


def collect_metadata_package(
    client: OSFClient,
    root: NodeRecord,
    node: NodeRecord,
    core_document: dict[str, Any],
    job: ExportJob,
    issues: list[ExportIssue],
    *,
    relationship_keys: set[str] | None = None,
    include_cedar: bool = True,
    issue_prefix: str = "",
) -> dict[str, Any]:
    raw = core_document.get("data") or {}
    relationships = raw.get("relationships") or {}
    resolved: dict[str, Any] = {}
    catalog: dict[str, Any] = {}

    for key, relationship in relationships.items():
        if not isinstance(relationship, dict):
            catalog[key] = {"status": "unrecognized", "value": relationship}
            continue
        url = related_href(relationship)
        catalog[key] = {
            "related_url": url,
            "linkage": relationship_linkage(relationship),
            "status": "link_only",
        }
        if key in {"root", "parent", "children"}:
            catalog[key]["status"] = "represented_in_project_hierarchy"
            continue
        if key in RELATIONSHIP_NOT_EXPANDED:
            catalog[key]["status"] = "not_expanded"
            catalog[key]["reason"] = RELATIONSHIP_NOT_EXPANDED[key]
            continue
        if key == "node_links" and "linked_nodes" in relationships:
            catalog[key]["status"] = "deprecated_alias_not_expanded"
            continue
        if (
            key not in METADATA_RELATIONSHIPS
            or (relationship_keys is not None and key not in relationship_keys)
            or not url
        ):
            continue
        check_cancel(job)
        try:
            source = collect_paginated_document(client, url)
            resolved[key] = source
            catalog[key]["status"] = "resolved"
            catalog[key]["record_count"] = source["record_count"]
        except ChecklistError as error:
            add_issue(
                issues,
                node,
                f"{issue_prefix}{METADATA_RELATIONSHIPS[key]} metadata relationship",
                key,
                error,
            )
            resolved[key] = {
                "status": "error",
                "source_url": url,
                "error": str(error),
                "data": [],
            }
            catalog[key]["status"] = "error"
            catalog[key]["error"] = str(error)

    custom_url = client.api_url(f"custom_item_metadata_records/{node.guid}/")
    try:
        custom_document = client.get_optional_json(custom_url, {404})
        custom: dict[str, Any] = (
            {"status": "complete", "source_url": custom_url, **custom_document}
            if custom_document is not None
            else {"status": "not_found", "source_url": custom_url, "data": None}
        )
    except ChecklistError as error:
        add_issue(
            issues, node, f"{issue_prefix}custom metadata record", node.guid, error
        )
        custom = {
            "status": "error",
            "source_url": custom_url,
            "error": str(error),
            "data": None,
        }

    cedar_url = client.api_url(f"nodes/{node.guid}/cedar_metadata_records/")
    if include_cedar:
        try:
            first_cedar = client.get_optional_json(cedar_url, {404, 405})
            cedar = (
                collect_paginated_document(client, cedar_url, first_cedar)
                if first_cedar is not None
                else {
                    "status": "not_available",
                    "source_url": cedar_url,
                    "record_count": 0,
                    "data": [],
                }
            )
        except ChecklistError as error:
            add_issue(
                issues, node, f"{issue_prefix}CEDAR metadata record", node.guid, error
            )
            cedar = {
                "status": "error",
                "source_url": cedar_url,
                "error": str(error),
                "data": [],
            }
    else:
        cedar = {
            "status": "not_requested",
            "source_url": cedar_url,
            "record_count": 0,
            "data": [],
        }

    related_count = sum(
        int(source.get("record_count") or 0)
        for source in resolved.values()
        if isinstance(source, dict)
    )
    related_count += int(cedar.get("record_count") or 0)
    if custom.get("status") == "complete" and custom.get("data"):
        related_count += 1
    node.related_metadata_count = related_count
    return {
        "format": "OSF comprehensive metadata export",
        "format_version": "1.0",
        "exported_utc": utc_now(),
        "exporter_version": SCRIPT_VERSION,
        "project": {
            "guid": node.guid,
            "title": node.title,
            "url": node.url,
            "public": node.public,
            "category": node.category,
            "root_guid": root.guid,
        },
        "hierarchy": {
            "root_guid": root.guid,
            "parent_guid": node.parent_guid,
            "children": [
                {"guid": child.guid, "title": child.title, "url": child.url}
                for child in node.children
            ],
        },
        "core_api_response": core_document,
        "custom_item_metadata": custom,
        "cedar_metadata_records": cedar,
        "resolved_relationships": resolved,
        "relationship_catalog": catalog,
        "notes": [
            "Relationship records are snapshots retrieved at export time; linked projects are not recursively exported from this document.",
            "The Storage Providers section describes configured providers but does not download project files.",
            "Wiki content and activity logs are handled by their separate export actions.",
            "View-only links are deliberately not retrieved because they can grant access to private content.",
        ],
    }


def complete_metadata_paths(node: NodeRecord) -> tuple[Path, Path]:
    stem = owner_prefixed_name(node, " - Complete Metadata")
    metadata_folder = action_output_folder(node, "metadata")
    return metadata_folder / f"{stem}.json", metadata_folder / f"{stem}.html"


def metadata_package_related_count(package: dict[str, Any]) -> int:
    resolved = package.get("resolved_relationships") or {}
    count = sum(
        int(source.get("record_count") or 0)
        for source in resolved.values()
        if isinstance(source, dict)
    )
    cedar = package.get("cedar_metadata_records") or {}
    if isinstance(cedar, dict):
        count += int(cedar.get("record_count") or 0)
    custom = package.get("custom_item_metadata") or {}
    if (
        isinstance(custom, dict)
        and custom.get("status") == "complete"
        and custom.get("data")
    ):
        count += 1
    return count


def export_metadata(
    client: OSFClient,
    root: NodeRecord,
    nodes: list[NodeRecord],
    job: ExportJob,
    issues: list[ExportIssue],
    report: ProgressCallback,
    start: float,
    span: float,
    all_nodes: list[NodeRecord] | None = None,
) -> tuple[int, int]:
    resources: list[dict[str, Any]] = []
    refreshed: dict[str, dict[str, Any]] = {}
    node_count = max(1, len(nodes))
    for index, node in enumerate(nodes):
        check_cancel(job)
        report(
            start + span * index / node_count,
            f"Comprehensive metadata: {node.display_name}",
        )
        try:
            document = client.get_json(client.api_url(f"nodes/{node.guid}/"))
            raw = document.get("data")
            if not isinstance(raw, dict):
                raise ChecklistError("OSF returned an unexpected metadata response")
            node.raw = raw
            assert node.folder is not None
            package = collect_metadata_package(
                client,
                root,
                node,
                document,
                job,
                issues,
                relationship_keys=None,
                include_cedar=True,
                issue_prefix="",
            )
            complete_json, complete_html = complete_metadata_paths(node)
            write_json_document(complete_json, package)
            complete_html.write_text(
                render_complete_metadata_html(package), encoding="utf-8"
            )
            refreshed[node.guid] = package
        except (ChecklistError, OSError) as error:
            add_issue(
                issues,
                node,
                "core metadata record",
                node.guid,
                error,
                critical=True,
            )

    aggregate_nodes = all_nodes if all_nodes is not None else nodes
    target_guids = {node.guid for node in nodes}
    for node in aggregate_nodes:
        package = refreshed.get(node.guid)
        if package is None:
            complete_json, _ = complete_metadata_paths(node)
            try:
                loaded = json.loads(complete_json.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("the existing package was not a JSON object")
                package = loaded
            except FileNotFoundError:
                if node.guid not in target_guids:
                    add_issue(
                        issues,
                        node,
                        "existing metadata package",
                        node.guid,
                        "The previously successful metadata package could not be found locally.",
                        critical=True,
                    )
                continue
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as error:
                add_issue(
                    issues,
                    node,
                    "existing metadata package",
                    node.guid,
                    error,
                    critical=True,
                )
                continue
        if package is None:
            continue
        raw = (package.get("core_api_response") or {}).get("data")
        if not isinstance(raw, dict):
            add_issue(
                issues,
                node,
                "existing metadata package",
                node.guid,
                "The complete metadata package did not contain its core API record.",
                critical=True,
            )
            continue
        node.raw = raw
        node.metadata_count = 1
        node.related_metadata_count = metadata_package_related_count(package)
        resources.append(package)

    report(start + span, "Comprehensive metadata export finished")
    return len(resources), sum(
        node.related_metadata_count for node in aggregate_nodes if node.metadata_count
    )


def wiki_page_details(wiki: dict[str, Any]) -> tuple[str, str]:
    attributes = wiki.get("attributes") or {}
    return (
        clean_title(attributes.get("name"), "Untitled wiki page"),
        str(wiki.get("id") or "unknown"),
    )


def wiki_filename(node: NodeRecord, wiki: dict[str, Any]) -> str:
    page_name, wiki_id = wiki_page_details(wiki)
    suffix = f" - Wiki - {safe_segment(page_name, 100)} [{wiki_id}].md"
    return owner_prefixed_name(node, suffix)


def wiki_version_sort_key(version: dict[str, Any]) -> tuple[int, Any, str]:
    version_id = str(version.get("id") or "")
    date_created = str((version.get("attributes") or {}).get("date_created") or "")
    if version_id.isdigit():
        return (0, int(version_id), date_created)
    return (1, date_created, version_id)


def wiki_version_filename(
    node: NodeRecord,
    wiki: dict[str, Any],
    version: dict[str, Any],
    width: int,
    fallback: int,
) -> str:
    page_name, wiki_id = wiki_page_details(wiki)
    version_id = str(version.get("id") or fallback)
    label = (
        version_id.zfill(width)
        if version_id.isdigit()
        else safe_segment(version_id, 30)
    )
    created = str(
        (version.get("attributes") or {}).get("date_created") or "date-unknown"
    )
    stamp = created[:19].replace(":", "-")
    suffix = (
        f" - Wiki - {safe_segment(page_name, 80)} [{wiki_id}]"
        f" - Version {label} - {safe_segment(stamp, 30)}.md"
    )
    return owner_prefixed_name(node, suffix)


def write_wiki_history_metadata(
    history_folder: Path,
    node: NodeRecord,
    wiki: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    page_name, wiki_id = wiki_page_details(wiki)
    stem = owner_prefixed_name(
        node,
        f" - Wiki - {safe_segment(page_name, 90)} [{wiki_id}] - Version History",
    )
    fields = [
        "version",
        "date_created",
        "user_guid",
        "size",
        "content_type",
        "filename",
        "download_status",
        "error",
        "api_url",
    ]
    with (history_folder / f"{stem}.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    document = {
        "wiki_page": {
            "id": wiki_id,
            "name": page_name,
            "owner_guid": node.guid,
            "owner_title": node.title,
        },
        "versions": rows,
    }
    with (history_folder / f"{stem}.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def get_node_wikis(
    client: OSFClient,
    node: NodeRecord,
    issues: list[ExportIssue],
) -> list[dict[str, Any]] | None:
    """Retrieve and sort the wiki pages belonging to one node."""
    relationships = node.raw.get("relationships") or {}
    wikis_url = related_href(relationships.get("wikis")) or client.api_url(
        f"nodes/{node.guid}/wikis/"
    )

    try:
        wikis = list(client.paginate(wikis_url))
    except ChecklistError as error:
        add_issue(
            issues,
            node,
            "wiki collection",
            node.guid,
            error,
            critical=True,
        )
        return None

    wikis.sort(
        key=lambda item: wiki_page_details(item)[0].casefold()
    )
    return wikis


def export_current_wiki_page(
    client: OSFClient,
    node: NodeRecord,
    wiki: dict[str, Any],
    issues: list[ExportIssue],
) -> bool:
    """Export the most recent content of one wiki page."""
    _, wiki_id = wiki_page_details(wiki)
    download_url = link_href(
        (wiki.get("links") or {}).get("download")
    )

    if not download_url and wiki_id != "unknown":
        download_url = client.api_url(
            f"wikis/{wiki_id}/content/"
        )

    try:
        if not isinstance(download_url, str) or not download_url:
            raise ChecklistError(
                "No current-content download link was provided"
            )

        content = client.get_text(download_url)
        assert node.folder is not None

        destination = (
            action_output_folder(node, "wiki_current")
            / wiki_filename(node, wiki)
        )
        destination.write_text(content, encoding="utf-8")

        node.wiki_count += 1
        return True
    except (ChecklistError, OSError) as error:
        add_issue(
            issues,
            node,
            "current wiki page",
            wiki_id,
            error,
        )
        return False


def export_one_wiki_version(
    client: OSFClient,
    node: NodeRecord,
    wiki: dict[str, Any],
    version: dict[str, Any],
    width: int,
    fallback: int,
    history_folder: Path,
    issues: list[ExportIssue],
) -> dict[str, Any]:
    """Export one historical wiki version and return its index row."""
    _, wiki_id = wiki_page_details(wiki)
    version_id = str(version.get("id") or fallback)
    filename = wiki_version_filename(
        node,
        wiki,
        version,
        width,
        fallback,
    )

    version_url = link_href(
        (version.get("links") or {}).get("download")
    )
    if not version_url and wiki_id != "unknown":
        version_url = client.api_url(
            f"wikis/{wiki_id}/versions/{version_id}/content/"
        )

    attributes = version.get("attributes") or {}
    relationships = version.get("relationships") or {}

    row: dict[str, Any] = {
        "version": version_id,
        "date_created": str(
            attributes.get("date_created") or ""
        ),
        "user_guid": (
            relationship_id(relationships.get("user")) or ""
        ),
        "size": attributes.get("size", ""),
        "content_type": str(
            attributes.get("content_type") or ""
        ),
        "filename": filename,
        "download_status": "failed",
        "error": "",
        "api_url": str(
            (version.get("links") or {}).get("self") or ""
        ),
    }

    try:
        if not isinstance(version_url, str) or not version_url:
            raise ChecklistError(
                "No version-content download link was provided"
            )

        content = client.get_text(version_url)
        (history_folder / filename).write_text(
            content,
            encoding="utf-8",
        )

        row["download_status"] = "downloaded"
        node.wiki_version_count += 1
    except (ChecklistError, OSError) as error:
        row["error"] = str(error)
        add_issue(
            issues,
            node,
            "wiki version",
            f"{wiki_id}:{version_id}",
            error,
        )

    return row


def export_wiki_history(
    client: OSFClient,
    node: NodeRecord,
    wiki: dict[str, Any],
    job: ExportJob,
    issues: list[ExportIssue],
) -> int:
    """Export every available version of one wiki page."""
    page_name, wiki_id = wiki_page_details(wiki)
    versions_url = related_href(
        (wiki.get("relationships") or {}).get("versions")
    )

    if not versions_url and wiki_id != "unknown":
        versions_url = client.api_url(
            f"wikis/{wiki_id}/versions/"
        )

    if not versions_url:
        add_issue(
            issues,
            node,
            "wiki version history",
            wiki_id,
            "No version-history link was provided",
        )
        return 0

    try:
        versions = sorted(
            client.paginate(versions_url),
            key=wiki_version_sort_key,
        )
    except ChecklistError as error:
        add_issue(
            issues,
            node,
            "wiki version history",
            wiki_id,
            error,
        )
        return 0

    if not versions:
        return 0

    assert node.folder is not None
    history_folder = (
        action_output_folder(node, "wiki_history")
        / f"{safe_segment(page_name, 120)} [{wiki_id}]"
    )

    try:
        history_folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        add_issue(
            issues,
            node,
            "wiki version history folder",
            wiki_id,
            error,
        )
        return 0

    numeric_widths = [
        len(str(version.get("id")))
        for version in versions
        if str(version.get("id") or "").isdigit()
    ]
    width = max([4, *numeric_widths])

    metadata_rows: list[dict[str, Any]] = []
    downloaded_count = 0

    for version_index, version in enumerate(
        versions,
        start=1,
    ):
        check_cancel(job)

        row = export_one_wiki_version(
            client,
            node,
            wiki,
            version,
            width,
            version_index,
            history_folder,
            issues,
        )
        metadata_rows.append(row)

        if row["download_status"] == "downloaded":
            downloaded_count += 1

    try:
        write_wiki_history_metadata(
            history_folder,
            node,
            wiki,
            metadata_rows,
        )
    except OSError as error:
        add_issue(
            issues,
            node,
            "wiki history index",
            wiki_id,
            error,
        )

    return downloaded_count


def export_node_wikis(
    client: OSFClient,
    node: NodeRecord,
    job: ExportJob,
    issues: list[ExportIssue],
    *,
    include_current: bool,
    include_history: bool,
) -> tuple[int, int]:
    """Export the selected wiki content for one node."""
    wikis = get_node_wikis(client, node, issues)
    if wikis is None:
        return 0, 0

    page_count = 0
    version_count = 0

    for wiki in wikis:
        check_cancel(job)

        if include_current and export_current_wiki_page(
            client,
            node,
            wiki,
            issues,
        ):
            page_count += 1

        if include_history:
            version_count += export_wiki_history(
                client,
                node,
                wiki,
                job,
                issues,
            )

    return page_count, version_count


def export_wikis(
    client: OSFClient,
    nodes: list[NodeRecord],
    job: ExportJob,
    issues: list[ExportIssue],
    report: ProgressCallback,
    start: float,
    span: float,
    *,
    include_current: bool,
    include_history: bool,
) -> tuple[int, int]:
    """Export the selected wiki content for a collection of nodes."""
    if not include_current and not include_history:
        raise ChecklistError(
            "Choose current wikis, wiki version history, or both."
        )

    page_total = 0
    version_total = 0
    node_count = max(1, len(nodes))

    for node_index, node in enumerate(nodes):
        check_cancel(job)
        report(
            start + span * node_index / node_count,
            f"Wikis: {node.display_name}",
        )

        page_count, version_count = export_node_wikis(
            client,
            node,
            job,
            issues,
            include_current=include_current,
            include_history=include_history,
        )
        page_total += page_count
        version_total += version_count

    if include_current and include_history:
        completion_message = "Wiki export finished"
    elif include_current:
        completion_message = "Current wiki export finished"
    else:
        completion_message = "Wiki version-history export finished"

    report(start + span, completion_message)
    return page_total, version_total


def log_origin_guid(log: dict[str, Any]) -> str | None:
    relationships = log.get("relationships") or {}
    original = relationship_id(relationships.get("original_node"))
    if original:
        return original
    params_node = ((log.get("attributes") or {}).get("params") or {}).get(
        "params_node"
    ) or {}
    if isinstance(params_node, dict) and params_node.get("id"):
        return str(params_node["id"]).lower()
    return relationship_id(relationships.get("node"))


def log_context_guid(log: dict[str, Any]) -> str | None:
    return relationship_id((log.get("relationships") or {}).get("node"))


def log_origin_title(log: dict[str, Any], fallback_guid: str) -> str:
    params = (log.get("attributes") or {}).get("params") or {}
    for key in ("params_node", "params_project"):
        value = params.get(key) or {}
        if isinstance(value, dict) and value.get("title"):
            return clean_title(value["title"], fallback_guid)
    return f"Former or inaccessible component {fallback_guid}"


def collect_logs(
    client: OSFClient,
    nodes: list[NodeRecord],
    job: ExportJob,
    issues: list[ExportIssue],
    report: ProgressCallback,
    start: float,
    span: float,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    node_count = max(1, len(nodes))
    for index, node in enumerate(nodes):
        check_cancel(job)
        report(start + span * index / node_count, f"Activity logs: {node.display_name}")
        logs_url = related_href((node.raw.get("relationships") or {}).get("logs"))
        logs_url = logs_url or client.api_url(f"nodes/{node.guid}/logs/")
        try:
            for log in client.paginate(logs_url):
                key = str(log.get("id") or f"missing-id-{len(by_id) + 1}")
                by_id.setdefault(key, log)
        except ChecklistError as error:
            add_issue(
                issues,
                node,
                "activity-log collection",
                node.guid,
                error,
                critical=True,
            )
    report(start + span, "Activity logs retrieved")
    return sorted(
        by_id.values(),
        key=lambda item: str((item.get("attributes") or {}).get("date") or ""),
        reverse=True,
    )


def log_csv_row(
    log: dict[str, Any], owner_guid: str, owner_title: str
) -> dict[str, str]:
    attributes = log.get("attributes") or {}
    relationships = log.get("relationships") or {}
    return {
        "date": str(attributes.get("date") or ""),
        "action": str(attributes.get("action") or ""),
        "log_id": str(log.get("id") or ""),
        "user_guid": relationship_id(relationships.get("user")) or "",
        "node_guid": owner_guid,
        "node_title": owner_title,
        "params_json": json.dumps(
            attributes.get("params") or {}, ensure_ascii=False, separators=(",", ":")
        ),
        "relationships_json": json.dumps(
            relationships, ensure_ascii=False, separators=(",", ":")
        ),
        "api_url": str((log.get("links") or {}).get("self") or ""),
    }


def write_log_files(
    folder: Path,
    display_name: str,
    owner_guid: str,
    owner_title: str,
    logs: list[dict[str, Any]],
) -> None:
    fields = [
        "date",
        "action",
        "log_id",
        "user_guid",
        "node_guid",
        "node_title",
        "params_json",
        "relationships_json",
        "api_url",
    ]
    with (folder / f"{display_name} - Activity Log.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for log in logs:
            writer.writerow(log_csv_row(log, owner_guid, owner_title))
    with (folder / f"{display_name} - Activity Log.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(logs, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


LogRecord = dict[str, Any]
LogsByNode = dict[str, list[LogRecord]]
FormerLogs = dict[tuple[str, str], list[LogRecord]]


def partition_logs(
    root: NodeRecord,
    logs: list[LogRecord],
    node_map: dict[str, NodeRecord],
    full_node_map: dict[str, NodeRecord],
) -> tuple[LogsByNode, FormerLogs, list[LogRecord]]:
    """Classify logs by their current, former, or unknown owner."""
    current: LogsByNode = {
        guid: [] for guid in node_map
    }
    former: FormerLogs = {}
    unassigned: list[LogRecord] = []

    for log in logs:
        origin_guid = log_origin_guid(log)

        if origin_guid in node_map:
            current[origin_guid].append(log)
            continue

        if origin_guid in full_node_map:
            # A targeted retry may encounter activity belonging to another
            # still-current component. Its successful file is left untouched.
            continue

        if origin_guid:
            parent_guid = log_context_guid(log)
            if parent_guid not in full_node_map:
                parent_guid = root.guid

            former.setdefault(
                (str(parent_guid), origin_guid),
                [],
            ).append(log)
            continue

        unassigned.append(log)

    return current, former, unassigned


def write_current_log_groups(
    nodes: list[NodeRecord],
    grouped_logs: LogsByNode,
    issues: list[ExportIssue],
) -> None:
    """Write one activity-log export for each current node."""
    for node in nodes:
        assert node.folder is not None

        node_logs = grouped_logs[node.guid]
        node.log_count = len(node_logs)

        try:
            write_log_files(
                action_output_folder(node, "logs"),
                node.display_name,
                node.guid,
                node.title,
                node_logs,
            )
        except OSError as error:
            add_issue(
                issues,
                node,
                "activity-log file",
                node.guid,
                error,
                critical=True,
            )


def write_former_log_groups(
    full_node_map: dict[str, NodeRecord],
    former_logs: FormerLogs,
    issues: list[ExportIssue],
) -> None:
    """Write logs belonging to former or inaccessible components."""
    for (
        parent_guid,
        origin_guid,
    ), component_logs in sorted(former_logs.items()):
        parent = full_node_map[parent_guid]
        assert parent.folder is not None

        title = log_origin_title(
            component_logs[0],
            origin_guid,
        )
        display_name = title_guid_name(title, origin_guid)
        folder = (
            action_output_folder(parent, "logs")
            / "Former or inaccessible components"
            / display_name
        )

        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_log_files(
                folder,
                display_name,
                origin_guid,
                title,
                component_logs,
            )
        except OSError as error:
            add_issue(
                issues,
                parent,
                "former-component activity-log file",
                origin_guid,
                error,
            )


def write_unassigned_log_group(
    root: NodeRecord,
    unassigned_logs: list[LogRecord],
    issues: list[ExportIssue],
) -> None:
    """Write logs for which OSF supplied no identifiable owner."""
    if not unassigned_logs:
        return

    assert root.folder is not None

    try:
        write_log_files(
            action_output_folder(root, "logs"),
            f"{root.display_name} - Unassigned",
            "",
            "Unassigned activity",
            unassigned_logs,
        )
    except OSError as error:
        add_issue(
            issues,
            root,
            "unassigned activity-log file",
            root.guid,
            error,
        )


def organize_logs(
    root: NodeRecord,
    nodes: list[NodeRecord],
    logs: list[LogRecord],
    issues: list[ExportIssue],
    all_nodes: list[NodeRecord] | None = None,
) -> int:
    """Classify activity logs and write them into the export tree."""
    node_map = {
        node.guid: node for node in nodes
    }
    full_node_map = (
        {node.guid: node for node in all_nodes}
        if all_nodes is not None
        else node_map
    )

    current, former, unassigned = partition_logs(
        root,
        logs,
        node_map,
        full_node_map,
    )

    write_current_log_groups(
        nodes,
        current,
        issues,
    )
    write_former_log_groups(
        full_node_map,
        former,
        issues,
    )
    write_unassigned_log_group(
        root,
        unassigned,
        issues,
    )

    return len(logs)


def provider_archive_details(record: dict[str, Any]) -> tuple[str, str, str]:
    attributes = record.get("attributes") or {}
    provider = str(
        attributes.get("provider")
        or attributes.get("name")
        or record.get("id")
        or "storage"
    ).casefold()
    links = record.get("links") or {}
    root_url = link_href(links.get("upload")) or link_href(links.get("download")) or ""
    return (
        provider,
        provider_label(provider),
        zip_download_url(root_url) if root_url else "",
    )


def export_file_archives(
    client: OSFClient,
    nodes: list[NodeRecord],
    job: ExportJob,
    issues: list[ExportIssue],
    report: ProgressCallback,
    start: float,
    span: float,
) -> tuple[int, int]:
    """Download one ZIP per configured storage provider into the node tree."""
    archives: list[tuple[NodeRecord, str, str, str]] = []
    expected_by_node: dict[str, int] = {}
    node_count = max(1, len(nodes))
    discovery_span = span * 0.15
    for index, node in enumerate(nodes):
        check_cancel(job)
        report(
            start + discovery_span * index / node_count,
            f"Finding storage providers: {node.display_name}",
        )
        files_url = related_href((node.raw.get("relationships") or {}).get("files"))
        files_url = files_url or client.api_url(f"nodes/{node.guid}/files/")
        try:
            records = list(client.paginate(files_url))
        except ChecklistError as error:
            add_issue(
                issues,
                node,
                "file-storage provider collection",
                node.guid,
                error,
                critical=True,
            )
            continue
        for record in records:
            expected_by_node[node.guid] = expected_by_node.get(node.guid, 0) + 1
            provider, label, download_url = provider_archive_details(record)
            if not download_url:
                add_issue(
                    issues,
                    node,
                    "file ZIP download link",
                    provider,
                    "OSF did not provide a ZIP-capable storage-provider link",
                )
                continue
            archives.append((node, provider, label, download_url))

    archive_count = 0
    total_bytes = 0
    succeeded_by_node: dict[str, int] = {}
    download_span = span - discovery_span
    archive_total = max(1, len(archives))
    
    for index, (node, provider, label, download_url) in enumerate(archives):
        # Give the file service a short break between Download as ZIP jobs.
        if index > 0:
            check_cancel(job)
            time.sleep(FILE_ARCHIVE_PAUSE_SECONDS)

        check_cancel(job)
        report(
            start + discovery_span + download_span * index / archive_total,
            f"Downloading {label} files: {node.display_name}",
        )
        assert node.folder is not None
        destination = action_output_folder(node, "files") / owner_prefixed_name(
            node,
            f" - Files - {safe_segment(label, 80)}.zip",
        )
        try:
            size = client.download_file(
                download_url,
                destination,
                cancelled=lambda: job.cancel_requested,
            )
            archive_count += 1
            total_bytes += size
            succeeded_by_node[node.guid] = (
                succeeded_by_node.get(node.guid, 0) + 1
            )
            node.file_archive_count += 1
            node.file_archive_bytes += size
        except (ChecklistError, OSError) as error:
            add_issue(issues, node, "file ZIP archive", provider, error)
    for node in nodes:
        expected = expected_by_node.get(node.guid, 0)
        if expected and succeeded_by_node.get(node.guid, 0) == 0:
            add_issue(
                issues,
                node,
                "file ZIP export",
                node.guid,
                "No storage-provider ZIP archive could be downloaded",
                critical=True,
            )
    report(start + span, "File ZIP downloads finished")
    return archive_count, total_bytes


def action_title(action: str) -> str:
    return {
        "metadata": "Comprehensive Metadata",
        "wiki_current": "Current Wikis",
        "wiki_history": "Wiki Version History",
        "logs": "Activity Logs",
        "files": "Files as ZIP",
    }[action]


def write_export_summary(
    root: NodeRecord,
    nodes: list[NodeRecord],
    job: ExportJob,
    counts: dict[str, int],
    issues: list[ExportIssue],
) -> Path:
    assert root.folder is not None
    summary_label = (
        f"{action_title(job.action)} {job.retry_mode.title()} Retry [{job.id}] Summary"
        if job.retry_of
        else f"{action_title(job.action)} Export Summary"
    )
    summary_folder = action_output_folder(root, job.action)
    summary_folder.mkdir(parents=True, exist_ok=True)

    path = summary_folder / owner_prefixed_name(
        root,
        f" - {summary_label}.json",
    )
    critical_count = sum(
        1 for issue in issues if issue.severity == "critical"
    )
    omission_count = sum(
        1 for issue in issues if issue.severity == "omission"
    )
    notes_by_action = {
        "metadata": [
            "Each Metadata folder contains readable comprehensive HTML and JSON.",
            "The export includes custom and CEDAR metadata plus supported relationship records.",
            "Separate copies of the underlying API responses are not created.",
            "View-only links and Storage Usage metadata are not retrieved.",
        ],
        "wiki_current": [
            "The most recent content of each wiki page is stored under Wikis/Current.",
        ],
        "wiki_history": [
            "Every available historical wiki version is stored under Wikis/Version History with CSV and JSON indexes.",
        ],
        "logs": [
            "Activity logs are stored in an Activity Logs folder inside each matching project/component folder.",
        ],
        "files": [
            "Each configured storage provider is downloaded as a separate ZIP archive.",
            "ZIP archives are stored in a Files folder inside the matching project/component folder.",
        ],
    }
    outcome = (
        "failed"
        if critical_count
        else "completed_with_omissions"
        if omission_count
        else "completed"
    )
    document = {
        "exporter_version": SCRIPT_VERSION,
        "action": job.action,
        "outcome": outcome,
        "retry": {
            "retry_of_job": job.retry_of,
            "mode": job.retry_mode,
            "scopes": job.retry_scopes,
            "description": job.retry_note,
        }
        if job.retry_of
        else None,
        "root_project": {
            "guid": root.guid,
            "title": root.title,
            "url": root.url,
        },
        "started_utc": job.started_at,
        "finished_utc": utc_now(),
        "counts": {
            "projects_and_components": len(nodes),
            **counts,
            "omitted_elements": omission_count,
            "critical_failures": critical_count,
        },
        "items": [
            {
                "guid": node.guid,
                "title": node.title,
                "parent_guid": node.parent_guid,
                "relative_folder": str(node.folder.relative_to(root.folder))
                if node.folder != root.folder
                else ".",
                "metadata_records": node.metadata_count,
                "resolved_related_metadata_records": node.related_metadata_count,
                "wiki_pages": node.wiki_count,
                "wiki_versions": node.wiki_version_count,
                "activity_log_entries": node.log_count,
                "file_zip_archives": node.file_archive_count,
                "file_zip_bytes": node.file_archive_bytes,
            }
            for node in nodes
        ],
        "issues": [issue.public_dict() for issue in issues],
        "issue_groups": group_export_issues(issues),
        "notes": [
            *notes_by_action[job.action],
            "File ZIP downloads run only when the separate Files as ZIP action is selected.",
        ],
    }
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def open_local_folder(path: Path) -> None:
    path = path.expanduser().resolve()
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def normalize_review_checks(
    roots: Iterable[NodeRecord], saved_checks: dict[str, bool]
) -> dict[str, bool]:
    """Represent review state at the top-level project-tree boundary."""
    normalized: dict[str, bool] = {}
    for root in roots:
        if saved_checks.get(root.guid):
            normalized.update({node.guid: True for node in flatten_tree(root)})
    return normalized


class ChecklistApp:
    def __init__(
        self,
        client: OSFClient,
        roots: list[NodeRecord],
        account_name: str,
        account_id: str,
        output_base: Path,
    ) -> None:
        self.client = client
        self.roots = roots
        self.root_map = {root.guid: root for root in roots}
        self.account_name = account_name
        self.account_id = account_id
        self.output_base = output_base
        self.output_base.mkdir(parents=True, exist_ok=True)
        self.session_key = secrets.token_urlsafe(32)
        self.jobs: dict[str, ExportJob] = {}
        self.job_order: list[str] = []
        self.lock = threading.RLock()
        self.work_queue: queue.Queue[str | None] = queue.Queue()
        self.state_path = (
            output_base / f".checklist-state-{safe_segment(account_id, 80)}.json"
        )
        saved_checks = self.load_checks()
        self.checks = normalize_review_checks(self.roots, saved_checks)
        if self.checks != saved_checks:
            self.save_checks()
        self.worker = threading.Thread(
            target=self.worker_loop, name="osf-export-worker", daemon=True
        )
        self.worker.start()

    def load_checks(self) -> dict[str, bool]:
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            checks = document.get("checks", {}) if isinstance(document, dict) else {}
            if isinstance(checks, dict):
                return {str(key): bool(value) for key, value in checks.items() if value}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_checks(self) -> None:
        temporary = self.state_path.with_name(self.state_path.name + ".part")
        document = {
            "account_id": self.account_id,
            "checks": self.checks,
            "updated_utc": utc_now(),
        }
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.state_path)

    def set_tree_check(self, root_guid: str, checked: bool) -> None:
        root = self.root_map.get(root_guid)
        if root is None:
            raise ChecklistError(
                f"Top-level project {root_guid} is not in this checklist."
            )
        tree_guids = [node.guid for node in flatten_tree(root)]
        with self.lock:
            if checked:
                self.checks.update({guid: True for guid in tree_guids})
            else:
                for guid in tree_guids:
                    self.checks.pop(guid, None)
            self.save_checks()

    def clear_checks(self) -> None:
        with self.lock:
            self.checks = {}
            self.save_checks()

    def queue_job(
        self,
        root_guid: str,
        action: str,
        *,
        retry_of: str = "",
        retry_mode: str = "",
        retry_scopes: dict[str, list[str]] | None = None,
        retry_note: str = "",
    ) -> ExportJob:
        if root_guid not in self.root_map:
            raise ChecklistError(f"Project {root_guid} is not in this checklist.")
        if action not in VALID_ACTIONS:
            raise ChecklistError(f"Unknown export action: {action}")
        root = self.root_map[root_guid]
        job = ExportJob(
            id=secrets.token_hex(8),
            root_guid=root_guid,
            root_title=root.title,
            action=action,
            retry_of=retry_of,
            retry_mode=retry_mode,
            retry_scopes=retry_scopes or {},
            retry_note=retry_note,
        )
        with self.lock:
            self.jobs[job.id] = job
            self.job_order.insert(0, job.id)
        self.work_queue.put(job.id)
        return job

    def retry_job(self, job_id: str) -> ExportJob:
        with self.lock:
            previous = self.jobs.get(job_id)
            if not previous:
                raise ChecklistError("The requested job was not found.")
            plan = build_retry_plan(
                previous.action,
                previous.issues,
                previous.status,
            )
            return self.queue_job(
                previous.root_guid,
                previous.action,
                retry_of=previous.id,
                retry_mode=str(plan["mode"]),
                retry_scopes=dict(plan["scopes"]),
                retry_note=str(plan["description"]),
            )

    def cancel_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise ChecklistError("The requested job was not found.")
            if job.status in {"queued", "running"}:
                job.cancel_requested = True
                job.message = "Cancellation requested"

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "jobs": [
                    self.jobs[job_id].public_dict()
                    for job_id in self.job_order
                    if job_id in self.jobs
                ],
            }

    def report(self, job: ExportJob, progress: float, message: str) -> None:
        with self.lock:
            job.progress = max(job.progress, min(1.0, max(0.0, progress)))
            job.message = message

    def worker_loop(self) -> None:
        while True:
            job_id = self.work_queue.get()
            try:
                if job_id is None:
                    return
                with self.lock:
                    job = self.jobs.get(job_id)
                if job:
                    self.run_job(job)
            finally:
                self.work_queue.task_done()

    def run_job(self, job: ExportJob) -> None:
        with self.lock:
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = utc_now()
                job.message = "Cancelled before starting"
                return
            job.status = "running"
            job.started_at = utc_now()
            job.message = "Reading project structure"
        issues: list[ExportIssue] = []
        counts = {
            "metadata_records": 0,
            "resolved_related_metadata_records": 0,
            "wiki_pages": 0,
            "wiki_versions": 0,
            "unique_activity_log_entries": 0,
            "file_zip_archives": 0,
            "file_zip_bytes": 0,
        }
        self.client.cancel_requested = lambda: job.cancel_requested
        try:
            root = discover_tree(self.client, job.root_guid)
            nodes = flatten_tree(root)
            root_folder = self.output_base / root.display_name
            ensure_hierarchy(root, root_folder, job.action)
            job.output_folder = str(root_folder)

            def callback(progress: float, message: str) -> None:
                self.report(job, progress, message)

            content_types = action_content_types(job.action)
            if job.retry_mode == "targeted":
                content_types = [
                    content_type
                    for content_type in content_types
                    if content_type in job.retry_scopes
                ]
            if not content_types:
                raise ChecklistError(
                    "The retry did not contain any recoverable export elements."
                )
            node_map = {node.guid: node for node in nodes}
            task_span = 0.91 / len(content_types)
            for task_index, content_type in enumerate(content_types):
                task_start = 0.03 + task_index * task_span
                if job.retry_mode == "targeted":
                    target_nodes = [
                        node_map[guid]
                        for guid in job.retry_scopes[content_type]
                        if guid in node_map
                    ]
                    missing = [
                        guid
                        for guid in job.retry_scopes[content_type]
                        if guid not in node_map
                    ]
                    if missing:
                        raise ChecklistError(
                            "The affected project/component is no longer present in "
                            f"the project structure: {', '.join(missing)}"
                        )
                else:
                    target_nodes = nodes

                if content_type == "metadata":
                    metadata_records, related_records = export_metadata(
                        self.client,
                        root,
                        target_nodes,
                        job,
                        issues,
                        callback,
                        task_start,
                        task_span,
                        all_nodes=nodes,
                    )
                    counts["metadata_records"] = metadata_records
                    counts["resolved_related_metadata_records"] = related_records
                elif content_type in {"wiki_current", "wiki_history"}:
                    pages, versions = export_wikis(
                        self.client,
                        target_nodes,
                        job,
                        issues,
                        callback,
                        task_start,
                        task_span,
                        include_current=content_type == "wiki_current",
                        include_history=content_type == "wiki_history",
                    )
                    counts["wiki_pages"] = pages
                    counts["wiki_versions"] = versions
                elif content_type == "logs":
                    logs = collect_logs(
                        self.client,
                        target_nodes,
                        job,
                        issues,
                        callback,
                        task_start,
                        task_span * 0.82,
                    )
                    self.report(
                        job, task_start + task_span * 0.86, "Organizing activity logs"
                    )
                    counts["unique_activity_log_entries"] = organize_logs(
                        root,
                        target_nodes,
                        logs,
                        issues,
                        all_nodes=nodes,
                    )
                    self.report(
                        job,
                        task_start + task_span,
                        "Activity-log export finished",
                    )
                elif content_type == "files":
                    archives, archive_bytes = export_file_archives(
                        self.client,
                        target_nodes,
                        job,
                        issues,
                        callback,
                        task_start,
                        task_span,
                    )
                    counts["file_zip_archives"] = archives
                    counts["file_zip_bytes"] = archive_bytes
            check_cancel(job)
            self.report(job, 0.95, "Writing export summary")
            try:
                write_export_summary(root, nodes, job, counts, issues)
            except OSError as error:
                add_issue(
                    issues,
                    root,
                    "export summary",
                    root.guid,
                    error,
                    critical=True,
                )
            critical_count = sum(1 for issue in issues if issue.severity == "critical")
            omission_count = sum(1 for issue in issues if issue.severity == "omission")
            affected_count = len(
                {issue.project_guid for issue in issues if issue.project_guid}
            )
            with self.lock:
                job.issues = issues
                if critical_count:
                    job.status = "failed"
                    job.message = (
                        "Failed — core metadata or the primary requested content could not "
                        "be exported. Recommend Retry."
                    )
                elif omission_count:
                    project_label = (
                        "project/component"
                        if affected_count == 1
                        else "projects/components"
                    )
                    issue_groups = group_export_issues(issues)
                    job.status = "completed_with_omissions"
                    base_message = (
                        f"Completed with omissions — {omission_count} "
                        f"{plural_element('element', omission_count)} of {affected_count} "
                        f"{project_label} failed to download"
                    )
                    job.message = (
                        f"{base_message} because {issue_groups[0]['reason']}."
                        if len(issue_groups) == 1
                        else f"{base_message} for {len(issue_groups)} reasons. See details."
                    )
                else:
                    job.status = "completed"
                    job.message = "Completed — all content succeeded."
                job.progress = 1.0
                job.finished_at = utc_now()
        except JobCancelled:
            with self.lock:
                job.status = "cancelled"
                job.message = (
                    "Cancelled. Completed outputs are retained and a retry can continue."
                )
                job.finished_at = utc_now()
                job.issues = issues

        # Keep the worker thread alive if an unexpected export defect occurs.
        except Exception as error:
            fallback_root = self.root_map[job.root_guid]
            add_issue(
                issues,
                fallback_root,
                "core metadata or primary requested content",
                job.root_guid,
                error,
                critical=True,
            )
            with self.lock:
                job.status = "failed"
                job.message = (
                    "Failed — core metadata or the primary requested content could not "
                    "be exported. Recommend Retry."
                )
                job.finished_at = utc_now()
                job.issues = issues

        finally:
            self.client.cancel_requested = None


def flatten_for_csv(
    nodes: list[NodeRecord], depth: int = 0, parent_guid: str = ""
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in nodes:
        rows.append(
            {
                "guid": node.guid,
                "title": node.title,
                "url": node.url,
                "visibility": node.visibility,
                "access_permission": node.permission,
                "depth": depth,
                "parent_guid": parent_guid,
            }
        )
        rows.extend(flatten_for_csv(node.children, depth + 1, node.guid))
    return rows


def render_node(
    node: NodeRecord,
    reviewed_guids: set[str],
    depth: int = 0,
    root: bool = False,
) -> str:
    title = html.escape(node.title)
    guid = html.escape(node.guid)
    url = html.escape(node.url, quote=True)
    category = html.escape(node.category.replace("_", " ").title())
    modified = html.escape(node.date_modified)
    visibility = node.visibility
    visibility_class = visibility.casefold()
    permission = html.escape(node.permission or "unknown")
    search = html.escape(
        f"{node.title} {node.guid} {node.category} {visibility} {node.permission}".casefold(),
        quote=True,
    )
    children_html = "".join(
        render_node(child, reviewed_guids, depth + 1, False) for child in node.children
    )
    toggle = (
        '<button class="toggle" type="button" aria-label="Expand or collapse">▾</button>'
        if node.children
        else '<span class="toggle-space"></span>'
    )
    batch = (
        '<label class="batch-label"><input class="batch-check" type="checkbox"> Include in batch</label>'
        if root
        else ""
    )
    actions = ""
    if root:
        buttons = "".join(
            f'<button class="action-button" type="button" data-guid="{guid}" data-action="{action}">{label}</button>'
            for action, label in (
                ("metadata", "Comprehensive metadata"),
                ("wiki_current", "Current wikis"),
                ("wiki_history", "Wiki history"),
                ("logs", "Activity logs"),
                ("files", "Download files as ZIP"),
            )
        )
        actions = f'<div class="row-actions">{buttons}</div>'
    reviewed = node.guid in reviewed_guids
    checked_attr = " checked" if reviewed else ""
    checked_class = " checked" if reviewed else ""
    review_control = (
        '<input class="node-check" type="checkbox" '
        f'aria-label="Mark {title} and its components reviewed"{checked_attr}>'
        if root
        else '<span class="review-check-space" aria-hidden="true"></span>'
    )
    metadata = (
        f"{guid} · {permission.title()} access"
        f"{f' · {category}' if category else ''}"
        f"{f' · Modified {modified}' if modified else ''}"
    )
    child_block = f'<ul class="children">{children_html}</ul>' if node.children else ""
    return f"""
<li class="node{checked_class}" data-guid="{guid}" data-public="{str(node.public).lower()}"
    data-permission="{permission}" data-search="{search}" data-depth="{depth}"
    data-root="{str(root).lower()}">
  <div class="node-row">
    {toggle}
    {review_control}
    <div class="node-info">
      <div class="title-line">
        <a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
        <span class="badge {visibility_class}">{visibility}</span>
        {batch}
      </div>
      <small>{metadata}</small>
      {actions}
    </div>
  </div>
  {child_block}
</li>
"""

# Kept as an embedded resource so the checklist remains a single-file tool.
# Isolating it here keeps the HTML renderer readable and makes the stylesheet
# straightforward to review or replace.
DASHBOARD_CSS = r"""
:root {
  --ink: #20313d;
  --muted: #657985;
  --line: #dbe4e9;
  --blue: #2c6e9f;
  --blue2: #194d72;
  --paper: #fff;
  --bg: #f3f7f9;
  --green: #e8f5eb;
  --orange: #fff1dc;
  --red: #a33b31;
  --purple: #6b4f8b;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font:
    16px/1.45 -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    Arial,
    sans-serif;
}
header {
  padding: 27px max(20px, calc((100% - 1240px) / 2));
  color: #fff;
  background: linear-gradient(135deg, #243746, #2c6e9f);
}
header h1 {
  margin: 0 0 3px;
  font-size: 2rem;
}
header p {
  margin: 0;
  opacity: 0.87;
}
main {
  width: min(1240px, calc(100% - 32px));
  margin: 0 auto;
  padding: 20px 0 60px;
}
.notice {
  padding: 13px 15px;
  margin-bottom: 15px;
  background: #fff5d8;
  border: 1px solid #e4ca75;
  border-radius: 8px;
}
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 11px;
  margin-bottom: 15px;
}
.stats div,
.toolbar,
.batch-panel,
.panel,
.jobs {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 9px;
  box-shadow: 0 4px 16px rgba(30, 50, 60, 0.06);
}
.stats div {
  padding: 14px;
}
.stats strong {
  display: block;
  font-size: 1.65rem;
}
.stats span,
.muted {
  color: var(--muted);
  font-size: 0.83rem;
}
.toolbar {
  position: sticky;
  top: 8px;
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px;
  margin-bottom: 15px;
}
.toolbar input[type="search"] {
  flex: 1 1 260px;
}
.toolbar input,
.toolbar select,
.toolbar button,
.batch-panel button,
.action-button,
.job button {
  padding: 8px 10px;
  background: #fff;
  border: 1px solid #b7c5cc;
  border-radius: 5px;
  font: inherit;
}
button {
  cursor: pointer;
}
button.primary {
  color: #fff;
  background: var(--blue);
  border-color: var(--blue);
}
button.danger {
  color: var(--red);
  border-color: #d6a9a4;
}
.progress {
  flex: 1 1 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--muted);
  font-size: 0.8rem;
}
.progress i,
.job-progress {
  height: 9px;
  flex: 1;
  overflow: hidden;
  background: #e6edf0;
  border-radius: 99px;
}
.progress b,
.job-progress b {
  display: block;
  width: 0;
  height: 100%;
  background: var(--blue);
}
.batch-panel,
.panel,
.jobs {
  padding: 17px;
  margin-bottom: 15px;
}
.batch-panel h2,
.panel h2,
.jobs h2 {
  margin: 0 0 10px;
}
.batch-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 11px 0;
}
.options {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  color: var(--muted);
  font-size: 0.88rem;
}
.options label,
.batch-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.export-choices {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 8px;
  margin: 13px 0;
}
.export-choice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: #f8fafb;
}
.export-choice input {
  margin-top: 3px;
}
.export-choice strong,
.export-choice small {
  display: block;
}
.export-choice small {
  margin-top: 2px;
  color: var(--muted);
}
.tree,
.children {
  padding: 0;
  margin: 0;
  list-style: none;
}
.children {
  margin-left: 31px;
  border-left: 2px solid #e0e8ec;
}
.node-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-height: 60px;
  padding: 9px;
  border-bottom: 1px solid #edf1f3;
}
.node-row:hover {
  background: #f7fafb;
}
.toggle {
  width: 24px;
  padding: 0;
  background: transparent;
  border: 0;
  cursor: pointer;
  font-size: 1rem;
}
.toggle-space {
  width: 24px;
}
.node-check {
  width: 18px;
  height: 18px;
  margin-top: 3px;
}
.review-check-space {
  flex: 0 0 18px;
  width: 18px;
}
.node-info {
  min-width: 0;
  flex: 1;
}
.title-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.node-info a {
  color: var(--blue2);
  font-weight: 700;
  text-decoration: none;
  overflow-wrap: anywhere;
}
.node-info a:hover {
  text-decoration: underline;
}
.node-info small {
  color: var(--muted);
}
.badge {
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 0.68rem;
  font-weight: 800;
}
.badge.public {
  color: #276137;
  background: var(--green);
}
.badge.private {
  color: #814c0b;
  background: var(--orange);
}
.batch-label {
  font-size: 0.76rem;
  color: var(--purple);
  font-weight: 650;
}
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.action-button {
  padding: 5px 8px;
  font-size: 0.78rem;
  color: var(--blue2);
}
.node.checked > .node-row {
  background: #f0f7f2;
}
.node.checked > .node-row .node-info > a {
  color: var(--muted);
  text-decoration: line-through;
}
.node.collapsed > .children {
  display: none;
}
.node.collapsed > .node-row .toggle {
  transform: rotate(-90deg);
}
.node.hidden {
  display: none;
}
.job {
  padding: 12px 0;
  border-top: 1px solid var(--line);
}
.job:first-of-type {
  border-top: 0;
}
.job-head,
.job-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.job-title {
  font-weight: 750;
}
.job-status {
  padding: 2px 7px;
  border-radius: 999px;
  background: #edf2f5;
  font-size: 0.72rem;
  font-weight: 800;
}
.job-status.completed {
  color: #276137;
  background: var(--green);
}
.job-status.failed,
.job-status.cancelled {
  color: var(--red);
  background: #f9e5e3;
}
.job-status.completed_with_omissions {
  color: #814c0b;
  background: var(--orange);
}
.job-progress {
  margin: 8px 0;
}
.job-message {
  color: var(--muted);
  font-size: 0.82rem;
}
.job-issues {
  margin: 7px 0;
  font-size: 0.78rem;
}
.job-issues summary {
  cursor: pointer;
  font-weight: 750;
}
.job-issues > ul > li {
  margin: 7px 0;
}
.job-issues li.critical {
  color: var(--red);
}
.job-issues li.omission {
  color: #814c0b;
}
.issue-elements {
  margin: 4px 0 7px;
  padding-left: 22px;
  color: var(--ink);
}
.issue-elements li {
  margin: 2px 0;
}
.retry-note {
  margin: 5px 0;
  color: var(--muted);
  font-size: 0.78rem;
}
.empty {
  color: var(--muted);
  font-style: italic;
}
footer {
  color: var(--muted);
  font-size: 0.8rem;
  text-align: center;
}
@media (max-width: 760px) {
  .stats {
    grid-template-columns: 1fr 1fr;
  }
  .children {
    margin-left: 13px;
  }
  .toolbar {
    position: static;
  }
}
""".strip()
def render_html(app: ChecklistApp) -> str:
    all_nodes = [node for root in app.roots for node in flatten_tree(root)]
    rows = flatten_for_csv(app.roots)
    public_count = sum(1 for node in all_nodes if node.public)
    private_count = len(all_nodes) - public_count
    reviewed_guids = {
        guid for guid, reviewed in app.checks.items() if reviewed
    }
    nodes_html = "".join(
        render_node(root, reviewed_guids, root=True) for root in app.roots
    )
    replacements = {
        "__DASHBOARD_CSS__": DASHBOARD_CSS,
        "__ACCOUNT__": html.escape(app.account_name),
        "__ROOT_COUNT__": f"{len(app.roots):,}",
        "__NODE_COUNT__": f"{len(all_nodes):,}",
        "__PUBLIC_COUNT__": f"{public_count:,}",
        "__PRIVATE_COUNT__": f"{private_count:,}",
        "__NODE_HTML__": nodes_html,
        "__SESSION_KEY__": json.dumps(app.session_key),
        "__OSF_STATUS_URL__": json.dumps(OSF_STATUS_URL),
        "__ROWS_JSON__": json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"),
        "__CHECKS_JSON__": json.dumps(app.checks, ensure_ascii=False).replace(
            "</", "<\\/"
        ),
        "__OUTPUT_BASE__": html.escape(str(app.output_base)),
        "__VERSION__": SCRIPT_VERSION,
    }
    template = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<title>OSF Export Checklist - Comprehensive Metadata</title>
<style>
__DASHBOARD_CSS__
</style>
</head>
<body>
<header><h1>OSF Export Checklist</h1><p>__ACCOUNT__ · <strong>Version __VERSION__</strong></p></header>
<main>
  <div class="notice"><strong>Local and private:</strong> the OSF token remains in the running Python process and is not placed in this page. Keep the terminal window open while exports run. The page contains private project titles and structure.</div>
  <section class="stats"><div><strong>__ROOT_COUNT__</strong><span>Top-level projects</span></div><div><strong>__NODE_COUNT__</strong><span>Projects and components</span></div><div><strong>__PUBLIC_COUNT__</strong><span>Public</span></div><div><strong>__PRIVATE_COUNT__</strong><span>Private</span></div></section>
  <section class="toolbar">
    <input id="search" type="search" placeholder="Search titles, GUIDs, or categories">
    <select id="visibility"><option value="all">Public and private</option><option value="public">Public only</option><option value="private">Private only</option></select>
    <select id="permission"><option value="all">All access permissions</option><option value="admin">Admin only</option><option value="write">Write only</option><option value="read">Read only</option></select>
    <select id="completion"><option value="all">Reviewed and unreviewed</option><option value="unchecked">Unreviewed only</option><option value="checked">Reviewed only</option></select>
    <button id="expand" type="button">Expand all</button><button id="collapse" type="button">Collapse all</button><button id="clear" type="button">Clear review checks</button><button id="csv" type="button">Export checklist CSV</button>
    <div class="progress"><span id="progressText">0 reviewed</span><i><b id="progressFill"></b></i></div>
  </section>
  <section class="batch-panel">
    <h2>Batch export</h2><p class="muted">Select “Include in batch” on one or more top-level projects, check the content to export, then run the batch. Filters control which projects are included by “Select all matching top-level projects.” A parent shown only to provide context for a matching component is not selected.</p>
    <div class="batch-buttons"><button id="selectAllBatch" type="button">Select all matching top-level projects</button><button id="clearBatch" type="button">Clear batch selection</button></div>
    <div class="export-choices">
      <label class="export-choice"><input type="checkbox" name="batchAction" value="metadata"><span><strong>Comprehensive metadata</strong><small>Per-item readable HTML and comprehensive JSON, including CEDAR/custom metadata and resolved relationships.</small></span></label>
      <label class="export-choice"><input type="checkbox" name="batchAction" value="wiki_current"><span><strong>Current wikis</strong><small>The most recent content of every wiki page.</small></span></label>
      <label class="export-choice"><input type="checkbox" name="batchAction" value="wiki_history"><span><strong>Wiki version history</strong><small>Every available historical wiki version with version indexes.</small></span></label>
      <label class="export-choice"><input type="checkbox" name="batchAction" value="logs"><span><strong>Activity logs</strong><small>Project and component activity records organized into the matching tree.</small></span></label>
      <label class="export-choice"><input type="checkbox" name="batchAction" value="files"><span><strong>Files as ZIP</strong><small>One ZIP archive per configured storage provider for each project and component in the selected trees.</small></span></label>
    </div>
    <div class="batch-buttons"><button class="primary" id="runBatch" type="button">Run selected exports</button></div>
    <div class="options"><span>Each checked content type runs independently and writes to its own named folder inside the matching project/component tree. An omission in one does not require rerunning the others. Jobs run sequentially, with only one export running at a time.</span></div>
    <p class="muted"><strong>Completed</strong> — all content succeeded. <strong>Completed with omissions</strong> — one or more identified elements could not be downloaded; expand the details for the exact titles, GUIDs, element IDs, and reasons. <strong>Failed</strong> — core metadata or the primary requested content could not be exported. <strong>Retry affected items</strong> reruns only the affected content type for the listed projects/components; failures that cannot be isolated require a full-action retry.</p>
  </section>
  <section class="jobs"><h2>Export activity</h2><div id="jobs"><div class="empty">No exports queued yet.</div></div></section>
  <section class="panel"><h2>Projects</h2><ul class="tree">__NODE_HTML__</ul></section>
  <footer>Exports are written beneath <strong>__OUTPUT_BASE__</strong>. Review checks are saved locally outside the browser; the OSF token is not saved.</footer>
</main>
<script>
(() => {
  "use strict";
  const KEY = __SESSION_KEY__,
    STATUS_URL = __OSF_STATUS_URL__,
    rows = __ROWS_JSON__;
  let checks = __CHECKS_JSON__;
  const nodes = [...document.querySelectorAll(".node")],
    nodeChecks = [
      ...document.querySelectorAll(
        '.node[data-root="true"] > .node-row .node-check',
      ),
    ];
  const search = document.getElementById("search"),
    visibility = document.getElementById("visibility"),
    permission = document.getElementById("permission"),
    completion = document.getElementById("completion");
  async function api(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OSF-Checklist-Key": KEY },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }
  function paintTree(check) {
    const root = check.closest('.node[data-root="true"]');
    const treeNodes = [root, ...root.querySelectorAll(".node")];
    treeNodes.forEach((node) => {
      node.classList.toggle("checked", check.checked);
      const guid = node.dataset.guid;
      if (check.checked) checks[guid] = true;
      else delete checks[guid];
    });
  }
  function progress() {
    const count = nodeChecks.filter((item) => item.checked).length,
      total = nodeChecks.length;
    document.getElementById("progressText").textContent =
      `${count.toLocaleString()} of ${total.toLocaleString()} top-level projects reviewed`;
    document.getElementById("progressFill").style.width =
      `${total ? (count / total) * 100 : 0}%`;
  }
  function filters() {
    const query = search.value.trim().toLowerCase();
    function evaluate(node) {
      const children = node.querySelector(":scope > .children"),
        childVisible = children
          ? [...children.children].map(evaluate).some(Boolean)
          : false,
        reviewed = node.classList.contains("checked"),
        kind = node.dataset.public === "true" ? "public" : "private";
      const own =
        (!query || node.dataset.search.includes(query)) &&
        (visibility.value === "all" || visibility.value === kind) &&
        (permission.value === "all" ||
          permission.value === node.dataset.permission) &&
        (completion.value === "all" ||
          completion.value === (reviewed ? "checked" : "unchecked"));
      node.dataset.filterMatch = own ? "true" : "false";

      if (node.dataset.root === "true") {
        const batchCheck = node.querySelector(
          ":scope > .node-row .batch-check",
        );
        if (batchCheck) {
          batchCheck.disabled = !own;
          if (!own) batchCheck.checked = false;
        }
      }
      const show = own || childVisible;
      node.classList.toggle("hidden", !show);
      if (childVisible && (query || permission.value !== "all"))
        node.classList.remove("collapsed");
      return show;
    }
    document
      .querySelectorAll(".tree")
      .forEach((tree) => [...tree.children].forEach(evaluate));
    const visibleRoots = [
      ...document.querySelectorAll(
        '.node[data-root="true"][data-filter-match="true"]:not(.hidden)',
      ),
    ].length;
    document.getElementById("selectAllBatch").textContent =
      `Select all matching top-level projects (${visibleRoots.toLocaleString()})`;
  }
  nodeChecks.forEach((check) => {
    const guid = check.closest(".node").dataset.guid;
    check.checked = Boolean(checks[guid]);
    paintTree(check);
    check.addEventListener("change", async () => {
      paintTree(check);
      progress();
      filters();
      try {
        await api("/api/checks", { guid, checked: check.checked });
      } catch (error) {
        alert(error.message);
      }
    });
  });
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest(".toggle");
    if (toggle) toggle.closest(".node").classList.toggle("collapsed");
  });
  search.addEventListener("input", filters);
  visibility.addEventListener("change", filters);
  permission.addEventListener("change", filters);
  completion.addEventListener("change", filters);
  document.getElementById("expand").onclick = () =>
    nodes.forEach((node) => node.classList.remove("collapsed"));
  document.getElementById("collapse").onclick = () =>
    nodes.forEach((node) => {
      if (node.querySelector(":scope > .children"))
        node.classList.add("collapsed");
    });
  document.getElementById("clear").onclick = async () => {
    if (!confirm("Clear every saved review checkbox?")) return;
    nodeChecks.forEach((check) => {
      check.checked = false;
      paintTree(check);
    });
    checks = {};
    progress();
    filters();
    try {
      await api("/api/checks/clear", {});
    } catch (error) {
      alert(error.message);
    }
  };
  async function queueJobs(guids, action) {
    if (!guids.length) {
      alert("Select at least one project for the batch export.");
      return;
    }
    try {
      await api("/api/jobs", { guids, action });
      await refreshJobs();
    } catch (error) {
      alert(error.message);
    }
  }
  document
    .querySelectorAll(".action-button")
    .forEach(
      (button) =>
        (button.onclick = () =>
          queueJobs([button.dataset.guid], button.dataset.action)),
    );
  document.getElementById("runBatch").onclick = async () => {
    const guids = [...document.querySelectorAll(".node[data-root=true]")]
      .filter(
        (node) => node.querySelector(":scope > .node-row .batch-check").checked,
      )
      .map((node) => node.dataset.guid);
    const actions = [
      ...document.querySelectorAll("input[name=batchAction]:checked"),
    ].map((input) => input.value);
    if (!guids.length) {
      alert("Select at least one project for the batch export.");
      return;
    }
    if (!actions.length) {
      alert("Check at least one export content option.");
      return;
    }
    try {
      await api("/api/jobs", { guids, actions });
      await refreshJobs();
    } catch (error) {
      alert(error.message);
    }
  };
  document.getElementById("selectAllBatch").onclick = () => {
    document
      .querySelectorAll(".node[data-root=true] > .node-row .batch-check")
      .forEach((check) => (check.checked = false));
    document
      .querySelectorAll(
        '.node[data-root="true"][data-filter-match="true"]:not(.hidden) > .node-row .batch-check',
      )
      .forEach((check) => (check.checked = true));
  };
  document.getElementById("clearBatch").onclick = () => {
    document
      .querySelectorAll(".node[data-root=true] .batch-check")
      .forEach((check) => (check.checked = false));
  };
  function escapeText(value) {
    const div = document.createElement("div");
    div.textContent = value ?? "";
    return div.innerHTML;
  }
  function renderJobs(jobs) {
    const area = document.getElementById("jobs");
    const openIssues = new Set(
      [...area.querySelectorAll(".job-issues[open]")].map(
        (details) => details.dataset.job,
      ),
    );
    const statusLabels = {
      completed: "Completed",
      completed_with_omissions: "Completed with omissions",
      failed: "Failed",
      cancelled: "Cancelled",
      queued: "Queued",
      running: "Running",
    };
    const actionLabels = {
      metadata: "Comprehensive metadata",
      wiki_current: "Current wikis",
      wiki_history: "Wiki version history",
      logs: "Activity logs",
      files: "Files as ZIP",
    };
    if (!jobs.length) {
      area.innerHTML = '<div class="empty">No exports queued yet.</div>';
      return;
    }
    area.innerHTML = jobs
      .map((job) => {
        const active = ["queued", "running"].includes(job.status);
        const retry = [
          "failed",
          "cancelled",
          "completed_with_omissions",
        ].includes(job.status);
        const groups = job.issue_groups || [];
        const issues = groups.length
          ? `<details class="job-issues" data-job="${escapeText(job.id)}"${openIssues.has(job.id) ? " open" : ""}><summary>${job.critical_failure_count || 0} critical failure(s), ${job.omission_count || 0} omission(s)</summary><ul>${groups.map((group) => `<li class="${escapeText(group.severity)}"><strong>${group.severity === "critical" ? "Critical" : "Omission"}:</strong> ${escapeText(group.message)}${String(group.reason || "").toLowerCase().includes("undergoing maintenance") ? ' <a href="' + escapeText(STATUS_URL) + '" target="_blank" rel="noopener noreferrer">Check OSF status</a>' : ""}<ul class="issue-elements">${(group.elements || []).map((element) => `<li>${escapeText(element.description)}</li>`).join("")}</ul></li>`).join("")}</ul></details>`
          : "";
        const plan = job.retry_plan || {};
        const retryLabel =
          plan.mode === "targeted"
            ? "Retry affected items"
            : "Retry full action";
        const retryNote =
          retry && plan.description
            ? `<div class="retry-note">${escapeText(plan.description)}</div>`
            : "";
        const retryKind = job.retry_of
          ? ` · ${escapeText(job.retry_mode)} retry`
          : "";
        return `<div class="job"><div class="job-head"><span class="job-title">${escapeText(job.root_title)} [${escapeText(job.root_guid)}] · ${escapeText(actionLabels[job.action] || job.action)}${retryKind}</span><span class="job-status ${escapeText(job.status)}">${escapeText(statusLabels[job.status] || job.status.replaceAll("_", " "))}</span></div><div class="job-progress"><b style="width:${Math.round(job.progress * 100)}%"></b></div><div class="job-foot"><span class="job-message">${escapeText(job.message)} · ${Math.round(job.progress * 100)}%</span><span>${active ? `<button data-cancel="${job.id}">Cancel</button>` : ""}${retry ? `<button data-retry="${job.id}">${retryLabel}</button>` : ""}${job.output_folder ? `<button data-open="${job.id}">Open folder</button>` : ""}</span></div>${retryNote}${issues}</div>`;
      })
      .join("");
    area.querySelectorAll("[data-cancel]").forEach(
      (button) =>
        (button.onclick = () =>
          api(`/api/jobs/${button.dataset.cancel}/cancel`, {})
            .then(refreshJobs)
            .catch((error) => alert(error.message))),
    );
    area.querySelectorAll("[data-retry]").forEach(
      (button) =>
        (button.onclick = () =>
          api(`/api/jobs/${button.dataset.retry}/retry`, {})
            .then(refreshJobs)
            .catch((error) => alert(error.message))),
    );
    area
      .querySelectorAll("[data-open]")
      .forEach(
        (button) =>
          (button.onclick = () =>
            api("/api/open-folder", { job_id: button.dataset.open }).catch(
              (error) => alert(error.message),
            )),
      );
  }
  async function refreshJobs() {
    try {
      const response = await fetch("/api/state", { cache: "no-store" }),
        data = await response.json();
      renderJobs(data.jobs || []);
    } catch (error) {
      console.warn("Could not refresh checklist state.", error);
    }
  }

  async function poll() {
    await refreshJobs();
    setTimeout(poll, 1500);
  }
  function csvEscape(value) {
    const text = String(value ?? "");
    return /[",\n]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
  }
  document.getElementById("csv").onclick = () => {
    const output = [
      [
        "guid",
        "title",
        "url",
        "visibility",
        "access_permission",
        "depth",
        "parent_guid",
        "reviewed",
      ],
    ];
    rows.forEach((row) =>
      output.push([
        row.guid,
        row.title,
        row.url,
        row.visibility,
        row.access_permission,
        row.depth,
        row.parent_guid,
        checks[row.guid] ? "Yes" : "No",
      ]),
    );
    const blob = new Blob(
        [output.map((row) => row.map(csvEscape).join(",")).join("\n")],
        { type: "text/csv;charset=utf-8" },
      ),
      url = URL.createObjectURL(blob),
      link = document.createElement("a");
    link.href = url;
    link.download = "OSF_Project_Checklist.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };
  progress();
  filters();
  poll();
})();
</script>
</body></html>"""
    marker_pattern = re.compile(
        "|".join(
            re.escape(marker)
            for marker in sorted(replacements, key=len, reverse=True)
        )
    )
    return marker_pattern.sub(
        lambda match: replacements[match.group(0)],
        template,
    )


def normalize_job_request(
    body: dict[str, Any], available_guids: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Validate and normalize a dashboard export request."""
    raw_actions = body.get("actions")
    if raw_actions is None:
        raw_action = body.get("action")
        raw_actions = [raw_action] if raw_action is not None else []
    if not isinstance(raw_actions, list):
        raise ChecklistError("Choose at least one export content option.")

    actions: list[str] = []
    seen_actions: set[str] = set()
    for action in raw_actions:
        normalized = str(action or "").strip()
        if normalized and normalized not in seen_actions:
            actions.append(normalized)
            seen_actions.add(normalized)
    if not actions:
        raise ChecklistError("Choose at least one export content option.")
    invalid_actions = [action for action in actions if action not in VALID_ACTIONS]
    if invalid_actions:
        raise ChecklistError(f"Unknown export action: {invalid_actions[0]}")

    raw_guids = body.get("guids")
    if not isinstance(raw_guids, list):
        raise ChecklistError("Choose at least one project.")
    normalized_guids: list[str] = []
    seen_guids: set[str] = set()
    for guid in raw_guids:
        normalized = str(guid or "").strip().lower()
        if normalized and normalized not in seen_guids:
            normalized_guids.append(normalized)
            seen_guids.add(normalized)
    if not normalized_guids:
        raise ChecklistError("Choose at least one project.")

    available = set(available_guids)
    missing = [guid for guid in normalized_guids if guid not in available]
    if missing:
        raise ChecklistError(f"Project is not in this checklist: {missing[0]}")
    return normalized_guids, actions


class ChecklistHandler(BaseHTTPRequestHandler):
    app: ChecklistApp

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, document: dict[str, Any]) -> None:
        self.send_bytes(
            status,
            json.dumps(document, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_bytes(
                200, render_html(self.app).encode("utf-8"), "text/html; charset=utf-8"
            )
        elif path == "/api/state":
            self.send_json(200, self.app.public_state())
        elif path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
        else:
            self.send_json(404, {"error": "Not found"})

    def authorized(self) -> bool:
        supplied = self.headers.get("X-OSF-Checklist-Key", "")
        return secrets.compare_digest(supplied, self.app.session_key)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ChecklistError("Invalid request length") from error
        if length < 0 or length > 1024 * 1024:
            raise ChecklistError("Request body is too large")
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChecklistError("Request body must be JSON") from error
        if not isinstance(document, dict):
            raise ChecklistError("Request body must be a JSON object")
        return document

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            self.send_json(
                403, {"error": "This local-control request was not authorized."}
            )
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/jobs":
                normalized_guids, actions = normalize_job_request(
                    body, self.app.root_map
                )
                jobs = [
                    self.app.queue_job(guid, action)
                    for guid in normalized_guids
                    for action in actions
                ]
                self.send_json(202, {"jobs": [job.public_dict() for job in jobs]})
                return
            if path == "/api/checks":
                guid = str(body.get("guid") or "").lower()
                if not guid:
                    raise ChecklistError("A top-level project GUID is required.")
                self.app.set_tree_check(guid, bool(body.get("checked")))
                self.send_json(200, {"ok": True})
                return
            if path == "/api/checks/clear":
                self.app.clear_checks()
                self.send_json(200, {"ok": True})
                return
            cancel_match = re.fullmatch(r"/api/jobs/([a-f0-9]+)/cancel", path)
            if cancel_match:
                self.app.cancel_job(cancel_match.group(1))
                self.send_json(200, {"ok": True})
                return
            retry_match = re.fullmatch(r"/api/jobs/([a-f0-9]+)/retry", path)
            if retry_match:
                job = self.app.retry_job(retry_match.group(1))
                self.send_json(202, {"job": job.public_dict()})
                return
            if path == "/api/open-folder":
                job_id = str(body.get("job_id") or "")
                with self.app.lock:
                    job = self.app.jobs.get(job_id)
                    folder = (
                        Path(job.output_folder)
                        if job and job.output_folder
                        else self.app.output_base
                    )
                resolved = folder.expanduser().resolve()
                if not resolved.is_relative_to(self.app.output_base.resolve()):
                    raise ChecklistError(
                        "The requested folder is outside the export location."
                    )
                open_local_folder(resolved)
                self.send_json(200, {"ok": True})
                return
            self.send_json(404, {"error": "Not found"})
        except ChecklistError as error:
            self.send_json(400, {"error": str(error)})
        except OSError as error:
            self.send_json(500, {"error": f"Local file error: {error}"})


def default_output_base() -> Path:
    downloads = Path.home() / "Downloads"
    parent = downloads if downloads.is_dir() else Path.cwd()
    return parent / "OSF Project Exports"


def get_token(explicit: str | None) -> str:
    token = (explicit or os.environ.get("OSF_TOKEN") or "").strip()
    if token:
        return token
    print()
    print("Paste your OSF Personal Access Token.")
    print("It will not be displayed in the checklist or saved to disk.")
    token = getpass.getpass("OSF token: ").strip()
    if not token:
        raise ChecklistError("No OSF token was provided.")
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a local OSF checklist with selectable metadata, wiki, activity-log, and per-project file ZIP exports."
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional number of recently modified top-level projects to show (default: all)",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="GUID_OR_URL",
        help="Show a specific project; repeat for several projects instead of loading the full inventory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Export folder (default: Downloads/OSF Project Exports)",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Local port (default: 8765)"
    )
    parser.add_argument(
        "--test", action="store_true", help="Use the OSF test environment"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open the checklist automatically"
    )
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION} (unified metadata, wikis, activity logs, permission filters, and per-project file ZIPs)",
    )
    return parser.parse_args(argv)


def choose_checklist_roots(
    client: OSFClient,
    explicit_projects: list[str],
    limit: int | None,
) -> list[NodeRecord]:
    if explicit_projects:
        roots: list[NodeRecord] = []
        seen: set[str] = set()
        for value in explicit_projects:
            guid = extract_guid(value)
            if guid not in seen:
                print(f"Loading project {guid}...", flush=True)
                roots.append(discover_tree(client, guid))
                seen.add(guid)
        return roots
    print("Retrieving the OSF project inventory...", flush=True)
    records = get_inventory(client)
    roots, orphans = build_hierarchy(records)
    candidates = [*roots, *orphans]
    candidates.sort(
        key=lambda item: (item.date_modified, item.title.casefold()), reverse=True
    )
    return candidates[:limit] if limit is not None else candidates


def start_server(
    app: ChecklistApp, requested_port: int
) -> tuple[ThreadingHTTPServer, int]:
    ChecklistHandler.app = app
    try:
        server = ThreadingHTTPServer(("127.0.0.1", requested_port), ChecklistHandler)
    except OSError:
        if requested_port == 0:
            raise
        print(
            f"Port {requested_port} is busy; selecting another local port.", flush=True
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChecklistHandler)
    server.daemon_threads = True
    return server, int(server.server_address[1])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"OSF Export Checklist {SCRIPT_VERSION}", flush=True)
    print(
        "Loading accessible projects, metadata, wikis, activity logs, and file ZIP actions.",
        flush=True,
    )
    print("Export jobs, including file ZIP downloads, run sequentially.", flush=True)
    if args.limit is not None and args.limit < 1:
        raise ChecklistError("--limit must be at least 1.")
    if not 0 <= args.port <= 65535:
        raise ChecklistError("--port must be between 0 and 65535.")
    token = get_token(args.token)
    project_hosts = {
        urllib.parse.urlparse(
            value if "://" in value else f"https://{value}"
        ).hostname
        for value in args.project
        if "/" in value or "." in value
    }

    if "test.osf.io" in project_hosts and "osf.io" in project_hosts:
        raise ChecklistError(
            "Do not mix production and test OSF project URLs in one checklist."
        )

    explicit_test_url = "test.osf.io" in project_hosts
    api_base = TEST_API if args.test or explicit_test_url else PRODUCTION_API
    client = OSFClient(api_base, token)
    print("Checking OSF account...", flush=True)
    account_name, account_id = get_account(client)
    print(f"Authenticated as: {account_name}", flush=True)
    roots = choose_checklist_roots(client, args.project, args.limit)
    if not roots:
        raise ChecklistError("OSF returned no accessible projects for this checklist.")
    output_base = (args.output or default_output_base()).expanduser().resolve()
    app = ChecklistApp(client, roots, account_name, account_id, output_base)
    server, port = start_server(app, args.port)
    url = f"http://127.0.0.1:{port}/"
    print()
    print(f"Top-level projects: {len(roots)}", flush=True)
    print(f"Exports folder: {output_base}", flush=True)
    print(f"Checklist: {url}", flush=True)
    print("Keep this window open. Press Control-C here when finished.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping the local checklist...", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        app.work_queue.put(None)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except ChecklistError as error:
        print(f"\nError: {error}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as error:
        print(f"\nFile error: {error}", file=sys.stderr)
        raise SystemExit(1)
