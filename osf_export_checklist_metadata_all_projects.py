#!/usr/bin/env python3
"""Run a local OSF comprehensive-metadata, wiki-history, and activity-log checklist.

The tool uses only the Python standard library. It keeps the OSF Personal
Access Token in the Python process, serves the checklist only on 127.0.0.1,
and preserves the OSF project/component hierarchy in every export.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import os
import queue
import re
import secrets
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
SCRIPT_VERSION = "0.4-descriptive-catalog"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 90
MAX_RETRIES = 6
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = f"OSF-Export-Checklist-Pilot/{SCRIPT_VERSION}"

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VALID_ACTIONS = {"metadata", "wikis", "logs", "everything"}

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
    "storage": "Storage Usage",
    "files": "Storage Providers",
}

RELATIONSHIP_NOT_EXPANDED = {
    "wikis": "Exported by the separate wiki action.",
    "logs": "Exported by the separate activity-log action.",
    "comments": "Comments are outside this metadata export.",
    "view_only_links": "Not expanded because view-only links can grant access to private content.",
}


class PilotError(RuntimeError):
    """A failure that can be shown directly to the user."""


class JobCancelled(PilotError):
    """Raised when a queued or running export is cancelled."""


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
    children: list["NodeRecord"] = field(default_factory=list)
    folder: Path | None = None
    metadata_count: int = 0
    related_metadata_count: int = 0
    wiki_count: int = 0
    wiki_version_count: int = 0
    log_count: int = 0

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
    make_zip: bool
    status: str = "queued"
    progress: float = 0.0
    message: str = "Waiting to start"
    errors: list[str] = field(default_factory=list)
    output_folder: str = ""
    zip_path: str = ""
    created_at: str = field(default_factory=lambda: utc_now())
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("cancel_requested", None)
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_title(value: Any, fallback: str) -> str:
    title = html.unescape(str(value or "")).strip()
    return title or fallback


def safe_segment(value: str, max_length: int = 140) -> str:
    value = unicodedata.normalize("NFC", str(value))
    value = INVALID_FILENAME_CHARS.sub(" - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "Untitled"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value


def title_guid_name(title: str, guid: str, max_length: int = 200) -> str:
    suffix = f" [{guid}]"
    safe_title = safe_segment(title, max_length=max(1, max_length - len(suffix)))
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


def add_page_size(url: str, size: int = PAGE_SIZE) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "page[size]" for key, _ in query):
        query.append(("page[size]", str(size)))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def extract_guid(value: str) -> str:
    value = value.strip()
    direct = re.fullmatch(r"([A-Za-z0-9]{5})(?:_v\d+)?", value)
    if direct:
        return direct.group(1).lower()
    parsed = urllib.parse.urlparse(value if "://" in value else f"https://{value}")
    for segment in [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]:
        match = re.fullmatch(r"([A-Za-z0-9]{5})(?:_v\d+)?", segment)
        if match:
            return match.group(1).lower()
    raise PilotError(f"Could not find a five-character OSF GUID in: {value}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the bearer token before urllib follows an untrusted redirect."""

    def __init__(self, trusted_hosts: set[str]) -> None:
        super().__init__()
        self.trusted_hosts = trusted_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlparse(newurl).hostname not in self.trusted_hosts:
            redirected.remove_header("Authorization")
        return redirected


class OSFClient:
    def __init__(self, api_base: str, token: str, timeout: int = REQUEST_TIMEOUT) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        api_host = urllib.parse.urlparse(self.api_base).hostname or ""
        self.trusted_hosts = {api_host}
        self.opener = urllib.request.build_opener(SafeRedirectHandler(self.trusted_hosts))

    def api_url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def headers(self, url: str, accept: str = "application/vnd.api+json") -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if urllib.parse.urlparse(url).hostname in self.trusted_hosts:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def open(self, url: str, accept: str = "application/vnd.api+json", extra: dict[str, str] | None = None):
        headers = self.headers(url, accept)
        headers.update(extra or {})
        request = urllib.request.Request(url, headers=headers, method="GET")
        return self.opener.open(request, timeout=self.timeout)

    def request_bytes(
        self,
        url: str,
        accept: str = "application/vnd.api+json",
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
                    time.sleep(delay)
                    continue
                raise http_error(error) from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt < MAX_RETRIES - 1:
                    time.sleep(min(30.0, 2.0**attempt))
                    continue
                raise PilotError("Could not reach OSF after several attempts.") from error
        raise PilotError(f"OSF request failed: {last_error}")

    def get_json(self, url: str) -> dict[str, Any]:
        raw = self.request_bytes(url)
        if raw is None:
            raise PilotError(f"OSF returned no response for {url}")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PilotError(f"OSF returned an unexpected response for {url}") from error
        if not isinstance(result, dict):
            raise PilotError(f"OSF returned an unexpected response for {url}")
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
            raise PilotError(f"OSF returned an unexpected response for {url}") from error
        if not isinstance(result, dict):
            raise PilotError(f"OSF returned an unexpected response for {url}")
        return result

    def get_text(self, url: str) -> str:
        raw = self.request_bytes(url, "text/markdown, text/plain;q=0.9, */*;q=0.1")
        if raw is None:
            raise PilotError(f"OSF returned no response for {url}")
        return raw.decode(
            "utf-8", errors="replace"
        )

    def paginate(self, url: str) -> Iterable[dict[str, Any]]:
        next_url: str | None = add_page_size(url)
        seen: set[str] = set()
        while next_url:
            if next_url in seen:
                raise PilotError(f"OSF repeated a pagination link: {next_url}")
            seen.add(next_url)
            document = self.get_json(next_url)
            data = document.get("data", [])
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise PilotError(f"Unexpected paginated response for {next_url}")
            for item in data:
                if isinstance(item, dict):
                    yield item
            next_value = (document.get("links") or {}).get("next")
            if isinstance(next_value, dict):
                next_value = next_value.get("href")
            next_url = next_value if isinstance(next_value, str) and next_value else None

def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(30.0, max(1.0, float(retry_after)))
    except (TypeError, ValueError):
        return min(30.0, 2.0**attempt)


def http_error(error: urllib.error.HTTPError) -> PilotError:
    try:
        body = error.read().decode("utf-8", errors="replace")[:800]
    except Exception:
        body = ""
    if error.code == 401:
        message = "OSF rejected the token (HTTP 401). Create a new osf.full_read token and try again."
    elif error.code == 403:
        message = "OSF denied access (HTTP 403). Confirm that the token can view this content."
    elif error.code == 404:
        message = "OSF could not find this project or resource (HTTP 404)."
    else:
        message = f"OSF request failed with HTTP {error.code}."
    if body:
        message += f" Response: {body}"
    return PilotError(message)


def node_from_api(record: dict[str, Any], parent_guid: str | None = None) -> NodeRecord:
    guid = str(record.get("id") or "").strip().lower()
    if not guid:
        raise PilotError("An OSF project/component response did not include a GUID.")
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
        parent_guid=parent_guid if parent_guid is not None else relationship_id(relationships.get("parent")),
    )


def get_account(client: OSFClient) -> tuple[str, str]:
    data = client.get_json(client.api_url("users/me/")).get("data") or {}
    attributes = data.get("attributes") or {}
    name = attributes.get("full_name") or attributes.get("given_name") or data.get("id") or "OSF user"
    return str(name), str(data.get("id") or "unknown")


def get_inventory(client: OSFClient) -> list[dict[str, Any]]:
    url = client.api_url(f"users/me/nodes/?{urllib.parse.urlencode({'page[size]': PAGE_SIZE})}")
    records: list[dict[str, Any]] = []
    for record in client.paginate(url):
        records.append(record)
        if len(records) % 100 == 0:
            print(f"Retrieved {len(records):,} projects/components...", flush=True)
    return records


def build_hierarchy(records: list[dict[str, Any]]) -> tuple[list[NodeRecord], list[NodeRecord]]:
    nodes: dict[str, NodeRecord] = {}
    for record in records:
        node = node_from_api(record)
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
    roots.sort(key=lambda item: (item.date_modified, item.title.casefold()), reverse=True)
    orphans.sort(key=lambda item: (item.date_modified, item.title.casefold()), reverse=True)
    return roots, orphans


def discover_tree(client: OSFClient, root_guid: str) -> NodeRecord:
    data = client.get_json(client.api_url(f"nodes/{root_guid}/")).get("data")
    if not isinstance(data, dict):
        raise PilotError("OSF returned an unexpected project response.")
    visited: set[str] = set()

    def visit(record: dict[str, Any], parent_guid: str | None) -> NodeRecord:
        node = node_from_api(record, parent_guid)
        if node.guid in visited:
            raise PilotError(f"The OSF hierarchy repeated GUID {node.guid}.")
        visited.add(node.guid)
        children_url = related_href((record.get("relationships") or {}).get("children"))
        if not children_url:
            children_url = client.api_url(f"nodes/{node.guid}/children/")
        children = list(client.paginate(children_url))
        children.sort(
            key=lambda item: (
                clean_title((item.get("attributes") or {}).get("title"), "").casefold(),
                str(item.get("id") or ""),
            )
        )
        for child in children:
            node.children.append(visit(child, node.guid))
        return node

    return visit(data, None)


def flatten_tree(root: NodeRecord) -> list[NodeRecord]:
    output: list[NodeRecord] = []

    def walk(node: NodeRecord) -> None:
        output.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return output


def ensure_hierarchy(root: NodeRecord, root_folder: Path, action: str) -> None:
    needs_metadata = action in {"metadata", "everything"}
    needs_wikis = action in {"wikis", "everything"}

    def create(node: NodeRecord, folder: Path) -> None:
        node.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        if needs_metadata:
            (folder / "Metadata").mkdir(exist_ok=True)
        if needs_wikis:
            (folder / "Wikis").mkdir(exist_ok=True)
        for child in node.children:
            create(child, folder / child.display_name)

    create(root, root_folder)


def owner_prefixed_name(node: NodeRecord, suffix: str, max_length: int = 240) -> str:
    return f"{title_guid_name(node.title, node.guid, max(20, max_length - len(suffix)))}{suffix}"


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
            raise PilotError(f"OSF repeated a pagination link: {next_url}")
        seen.add(next_url)
        current = document if document is not None else client.get_json(next_url)
        document = None
        data = current.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if data is None:
            data = []
        if not isinstance(data, list):
            raise PilotError(f"Unexpected metadata response for {next_url}")
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
    embedded_user = (((record.get("embeds") or {}).get("users") or {}).get("data") or {})
    if isinstance(embedded_user, dict):
        user_attributes = embedded_user.get("attributes") or {}
        if user_attributes.get("full_name"):
            return str(user_attributes["full_name"])
    return str(record.get("id") or record.get("type") or "record")


def relationship_records(package: dict[str, Any], key: str) -> list[dict[str, Any]]:
    source = (package.get("resolved_relationships") or {}).get(key) or {}
    data = source.get("data") or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def joined_relationship_labels(package: dict[str, Any], key: str) -> str:
    return "; ".join(metadata_record_label(record) for record in relationship_records(package, key))


def metadata_csv_row(
    node: NodeRecord,
    raw: dict[str, Any],
    root_guid: str,
    package: dict[str, Any],
) -> dict[str, str]:
    attributes = raw.get("attributes") or {}
    relationships = raw.get("relationships") or {}
    links = raw.get("links") or {}
    permissions = attributes.get("current_user_permissions") or []
    tags = attributes.get("tags") or []
    custom_document = package.get("custom_item_metadata") or {}
    custom_data = custom_document.get("data") or {}
    custom_attributes = custom_data.get("attributes") or {} if isinstance(custom_data, dict) else {}
    funders = custom_attributes.get("funders") or []
    funder_labels: list[str] = []
    if isinstance(funders, list):
        for funder in funders:
            if not isinstance(funder, dict):
                funder_labels.append(str(funder))
                continue
            name = str(funder.get("funder_name") or funder.get("name") or "Unnamed funder")
            award = str(funder.get("award_number") or "").strip()
            funder_labels.append(f"{name} ({award})" if award else name)
    return {
        "guid": node.guid,
        "title": clean_title(attributes.get("title"), node.title),
        "url": str(links.get("html") or node.url),
        "root_guid": root_guid,
        "parent_guid": relationship_id(relationships.get("parent")) or "",
        "public": "Yes" if bool(attributes.get("public")) else "No",
        "category": str(attributes.get("category") or ""),
        "description": str(attributes.get("description") or ""),
        "date_created": str(attributes.get("date_created") or ""),
        "date_modified": str(attributes.get("date_modified") or ""),
        "registration": "Yes" if bool(attributes.get("registration")) else "No",
        "tags": "; ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags),
        "subjects": "; ".join(str(value) for value in (attributes.get("subjects") or [])),
        "language": str(custom_attributes.get("language") or ""),
        "resource_type_general": str(custom_attributes.get("resource_type_general") or ""),
        "funders": "; ".join(funder_labels),
        "contributors": joined_relationship_labels(package, "contributors"),
        "affiliated_institutions": joined_relationship_labels(package, "affiliated_institutions"),
        "identifiers": joined_relationship_labels(package, "identifiers"),
        "license": joined_relationship_labels(package, "license"),
        "registrations": joined_relationship_labels(package, "registrations"),
        "linked_projects": joined_relationship_labels(package, "linked_nodes"),
        "linked_registrations": joined_relationship_labels(package, "linked_registrations"),
        "preprints": joined_relationship_labels(package, "preprints"),
        "storage_providers": joined_relationship_labels(package, "files"),
        "current_user_permissions": "; ".join(str(value) for value in permissions)
        if isinstance(permissions, list)
        else str(permissions),
        "api_url": str(links.get("self") or ""),
    }


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
        items = "".join(f"<li>{metadata_html_value(item, depth + 1)}</li>" for item in value)
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
            f'<summary>{html.escape(label)} <small>#{index}</small></summary>'
            f'{metadata_html_value(record)}'
            '</details>'
        )
    return "".join(output)


def render_complete_metadata_html(package: dict[str, Any]) -> str:
    project = package.get("project") or {}
    title = str(project.get("title") or "Untitled OSF project")
    guid = str(project.get("guid") or "unknown")
    core_document = package.get("core_api_response") or {}
    core = core_document.get("data") or {}
    attributes = core.get("attributes") or {} if isinstance(core, dict) else {}
    links = core.get("links") or {} if isinstance(core, dict) else {}
    custom = package.get("custom_item_metadata") or {"status": "not_found"}
    custom_display = custom.get("data") if custom.get("status") == "complete" else custom
    cedar = package.get("cedar_metadata_records") or {"status": "not_available"}
    resolved = package.get("resolved_relationships") or {}
    relationship_sections = "".join(
        f'<section><h2>{html.escape(METADATA_RELATIONSHIPS.get(key, humanize_metadata_key(key)))}</h2>'
        f'<p class="source">Source: {metadata_html_value(source.get("source_url"))}</p>'
        f'{metadata_records_html(source)}</section>'
        for key, source in resolved.items()
        if isinstance(source, dict)
    )
    catalog = package.get("relationship_catalog") or {}
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<title>{html.escape(title)} [{html.escape(guid)}] - Complete Metadata</title>
<style>
:root{{--ink:#243746;--muted:#657985;--line:#d8e2e7;--blue:#1f608d;--bg:#f4f7f9;--paper:#fff;--warn:#8a4b08}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.48 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
header{{padding:28px max(22px,calc((100% - 1040px)/2));color:#fff;background:linear-gradient(135deg,#243746,#2c6e9f)}}
header h1{{margin:0 0 5px;font-size:1.8rem}}header p{{margin:0;opacity:.88}}main{{width:min(1040px,calc(100% - 30px));margin:20px auto 60px}}
section{{margin:0 0 16px;padding:18px;background:var(--paper);border:1px solid var(--line);border-radius:9px;box-shadow:0 3px 12px rgba(20,45,60,.05)}}
h2{{margin:0 0 11px;font-size:1.18rem}}a{{color:var(--blue);overflow-wrap:anywhere}}.metadata-table{{width:100%;border-collapse:collapse}}
.metadata-table th,.metadata-table td{{padding:7px 9px;border:1px solid var(--line);text-align:left;vertical-align:top}}.metadata-table th{{width:25%;background:#f5f8fa}}
.metadata-table.nested th{{width:28%;font-size:.84rem}}.metadata-list{{margin:0;padding-left:21px}}.metadata-list>li{{margin:4px 0}}
.record{{margin:8px 0;border:1px solid var(--line);border-radius:6px}}.record summary{{padding:8px 10px;cursor:pointer;font-weight:700;background:#f6f9fa}}
.record>.metadata-table{{margin:8px;width:calc(100% - 16px)}}.empty-value,.source,footer{{color:var(--muted)}}.source{{font-size:.78rem}}
.warning{{color:var(--warn)}}.text-value{{white-space:pre-wrap;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.78rem}}
footer{{text-align:center;font-size:.78rem}}@media(max-width:650px){{.metadata-table th{{width:35%}}}}
</style></head><body>
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
<footer>Generated locally by OSF Export Checklist {html.escape(SCRIPT_VERSION)}. The complete JSON and normalized source records are stored beside this document.</footer>
</main></body></html>'''


def first_nonempty(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def find_orcid(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if "orcid" in str(key).casefold():
                found = find_orcid(item)
                if found:
                    return found
        for item in value.values():
            found = find_orcid(item)
            if found:
                return found
        return ""
    if isinstance(value, list):
        for item in value:
            found = find_orcid(item)
            if found:
                return found
        return ""
    text = str(value or "")
    match = re.search(r"(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-[\dX]{4})", text, re.I)
    return f"https://orcid.org/{match.group(1).upper()}" if match else ""


def embedded_user(record: dict[str, Any]) -> dict[str, Any]:
    user = (((record.get("embeds") or {}).get("users") or {}).get("data") or {})
    return user if isinstance(user, dict) else {}


def descriptive_contributor(record: dict[str, Any]) -> dict[str, Any]:
    contributor_attributes = record.get("attributes") or {}
    user = embedded_user(record)
    user_attributes = user.get("attributes") or {}
    name = (
        first_nonempty(user_attributes, "full_name", "fullName")
        or first_nonempty(contributor_attributes, "full_name", "fullName", "unregistered_contributor")
        or metadata_record_label(record)
    )
    given_name = first_nonempty(user_attributes, "given_name", "givenName")
    family_name = first_nonempty(user_attributes, "family_name", "familyName")
    user_relationship = (record.get("relationships") or {}).get("users") or {}
    user_guid = str(user.get("id") or relationship_id(user_relationship) or "")
    user_links = user.get("links") or {}
    profile_url = str(user_links.get("html") or "")
    if not profile_url and user_guid:
        profile_url = f"https://osf.io/{user_guid}/"
    name_identifiers: list[dict[str, str]] = []
    if user_guid:
        name_identifiers.append(
            {
                "nameIdentifier": profile_url or user_guid,
                "nameIdentifierScheme": "OSF",
                "schemeUri": "https://osf.io/",
            }
        )
    orcid = find_orcid(user_attributes)
    if orcid:
        name_identifiers.append(
            {
                "nameIdentifier": orcid,
                "nameIdentifierScheme": "ORCID",
                "schemeUri": "https://orcid.org/",
            }
        )
    result: dict[str, Any] = {
        "name": name,
        "nameType": "Personal",
        "nameIdentifiers": name_identifiers,
    }
    if given_name:
        result["givenName"] = given_name
    if family_name:
        result["familyName"] = family_name
    if profile_url:
        result["osfProfile"] = profile_url
    if contributor_attributes.get("permission"):
        result["osfPermission"] = contributor_attributes["permission"]
    if "bibliographic" in contributor_attributes:
        result["bibliographic"] = bool(contributor_attributes.get("bibliographic"))
    return result


def descriptive_identifier(record: dict[str, Any]) -> dict[str, Any]:
    attributes = record.get("attributes") or {}
    value = first_nonempty(attributes, "value", "identifier", "doi") or str(record.get("id") or "")
    category = first_nonempty(attributes, "category", "identifier_type", "identifierType")
    lower_value = value.casefold()
    lower_category = category.casefold()
    if "doi" in lower_category or "doi.org/" in lower_value or value.startswith("10."):
        identifier_type = "DOI"
        value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
        value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    elif "ark" in lower_category or "/ark:/" in lower_value:
        identifier_type = "ARK"
    elif re.match(r"^https?://", value):
        identifier_type = "URL"
    else:
        identifier_type = category.upper() if category else "Other"
    return {"identifier": value, "identifierType": identifier_type}


def descriptive_subjects(attributes: dict[str, Any]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, scheme: str) -> None:
        if isinstance(value, dict):
            text = first_nonempty(value, "text", "title", "name", "label")
            identifier = first_nonempty(value, "id", "value")
        else:
            text = str(value or "").strip()
            identifier = ""
        if not text or text.casefold() in seen:
            return
        seen.add(text.casefold())
        entry = {"subject": text, "subjectScheme": scheme}
        if identifier:
            entry["valueUri"] = identifier
        output.append(entry)

    subjects = attributes.get("subjects") or []
    if isinstance(subjects, list):
        for subject in subjects:
            add(subject, "OSF subject")
    tags = attributes.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            add(tag, "OSF tag")
    return output


def descriptive_funding(custom_attributes: dict[str, Any]) -> list[dict[str, str]]:
    funders = custom_attributes.get("funders") or []
    if not isinstance(funders, list):
        return []
    output: list[dict[str, str]] = []
    for funder in funders:
        if not isinstance(funder, dict):
            output.append({"funderName": str(funder)})
            continue
        mapped = {
            "funderName": first_nonempty(funder, "funder_name", "funderName", "name"),
            "funderIdentifier": first_nonempty(
                funder, "funder_identifier", "funderIdentifier", "identifier"
            ),
            "funderIdentifierType": first_nonempty(
                funder, "funder_identifier_type", "funderIdentifierType"
            ),
            "awardNumber": first_nonempty(funder, "award_number", "awardNumber"),
            "awardUri": first_nonempty(funder, "award_uri", "awardUri"),
            "awardTitle": first_nonempty(funder, "award_title", "awardTitle"),
        }
        output.append({key: value for key, value in mapped.items() if value})
    return output


def related_record_identifier(record: dict[str, Any]) -> tuple[str, str]:
    attributes = record.get("attributes") or {}
    links = record.get("links") or {}
    doi = first_nonempty(attributes, "doi")
    if doi:
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I), "DOI"
    url = str(links.get("html") or "")
    if url:
        return url, "URL"
    value = first_nonempty(attributes, "value", "identifier")
    if value:
        return value, descriptive_identifier(record)["identifierType"]
    identifier = str(record.get("id") or "")
    record_type = str(record.get("type") or "")
    if identifier and record_type in {"nodes", "registrations", "preprints"}:
        return f"https://osf.io/{identifier}/", "URL"
    return identifier, "Other"


def descriptive_relationships(package: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(identifier: str, identifier_type: str, relation_type: str, information: str) -> None:
        if not identifier:
            return
        key = (identifier, identifier_type, relation_type)
        if key in seen:
            return
        seen.add(key)
        output.append(
            {
                "relatedIdentifier": identifier,
                "relatedIdentifierType": identifier_type,
                "relationType": relation_type,
                "relationTypeInformation": information,
            }
        )

    hierarchy = package.get("hierarchy") or {}
    parent_guid = str(hierarchy.get("parent_guid") or "")
    if parent_guid:
        add(f"https://osf.io/{parent_guid}/", "URL", "IsPartOf", "OSF parent project or component")
    for child in hierarchy.get("children") or []:
        if isinstance(child, dict) and child.get("guid"):
            add(
                f"https://osf.io/{child['guid']}/",
                "URL",
                "HasPart",
                "OSF component",
            )

    mappings = {
        "registrations": ("HasVersion", "OSF registration"),
        "linked_nodes": ("Other", "OSF linked project"),
        "linked_registrations": ("Other", "OSF linked registration"),
        "linked_by_nodes": ("Other", "OSF project linking to this resource"),
        "linked_by_registrations": ("Other", "OSF registration linking to this resource"),
        "preprints": ("Other", "OSF linked preprint"),
        "forked_from": ("IsDerivedFrom", "OSF source project"),
        "template_node": ("IsDerivedFrom", "OSF template project"),
        "forks": ("IsSourceOf", "OSF fork"),
    }
    for relationship_key, (relation_type, information) in mappings.items():
        for record in relationship_records(package, relationship_key):
            identifier, identifier_type = related_record_identifier(record)
            add(identifier, identifier_type, relation_type, information)
    return output


def descriptive_metadata_record(package: dict[str, Any]) -> dict[str, Any]:
    project = package.get("project") or {}
    core = (package.get("core_api_response") or {}).get("data") or {}
    attributes = core.get("attributes") or {}
    custom_document = package.get("custom_item_metadata") or {}
    custom_data = custom_document.get("data") or {}
    custom_attributes = custom_data.get("attributes") or {} if isinstance(custom_data, dict) else {}
    hierarchy = package.get("hierarchy") or {}

    all_contributor_records = relationship_records(package, "contributors")
    bibliographic_records = relationship_records(package, "bibliographic_contributors")
    if not bibliographic_records:
        bibliographic_records = [
            record
            for record in all_contributor_records
            if bool((record.get("attributes") or {}).get("bibliographic"))
        ]
    creators = [descriptive_contributor(record) for record in bibliographic_records]
    creator_names = {str(item.get("name") or "").casefold() for item in creators}
    contributors = []
    for record in all_contributor_records:
        contributor = descriptive_contributor(record)
        if str(contributor.get("name") or "").casefold() not in creator_names:
            contributor["contributorType"] = "Other"
            contributors.append(contributor)

    identifiers = [
        descriptive_identifier(record)
        for record in relationship_records(package, "identifiers")
    ]
    identifiers.insert(
        0,
        {
            "identifier": str(project.get("url") or f"https://osf.io/{project.get('guid')}/"),
            "identifierType": "URL",
        },
    )
    identifiers.append({"identifier": str(project.get("guid") or ""), "identifierType": "OSF GUID"})

    rights_list: list[dict[str, Any]] = []
    for license_record in relationship_records(package, "license"):
        license_attributes = license_record.get("attributes") or {}
        rights = {
            "rights": first_nonempty(license_attributes, "name", "title")
            or metadata_record_label(license_record),
            "rightsUri": first_nonempty(license_attributes, "url"),
            "rightsIdentifier": str(license_record.get("id") or ""),
        }
        rights_list.append({key: value for key, value in rights.items() if value})
    node_license = attributes.get("node_license") or {}
    rights_holders = node_license.get("copyright_holders") or []

    institutions = []
    for institution in relationship_records(package, "affiliated_institutions"):
        institution_attributes = institution.get("attributes") or {}
        entry = {
            "name": first_nonempty(institution_attributes, "name", "title")
            or metadata_record_label(institution),
            "affiliationIdentifier": first_nonempty(
                institution_attributes, "ror_uri", "ror", "identifier"
            ),
            "affiliationIdentifierScheme": "ROR"
            if first_nonempty(institution_attributes, "ror_uri", "ror")
            else "",
        }
        institutions.append({key: value for key, value in entry.items() if value})

    description = str(attributes.get("description") or "")
    dates = []
    if attributes.get("date_created"):
        dates.append({"date": str(attributes["date_created"]), "dateType": "Created"})
    if attributes.get("date_modified"):
        dates.append({"date": str(attributes["date_modified"]), "dateType": "Updated"})

    return {
        "identifiers": identifiers,
        "creators": creators,
        "titles": [{"title": str(project.get("title") or attributes.get("title") or "Untitled")}],
        "publisher": {"name": "OSF"},
        "subjects": descriptive_subjects(attributes),
        "contributors": contributors,
        "dates": dates,
        "language": str(custom_attributes.get("language") or ""),
        "types": {
            "resourceTypeGeneral": str(
                custom_attributes.get("resource_type_general") or "Project"
            ),
            "resourceType": str(project.get("category") or attributes.get("category") or ""),
        },
        "relatedIdentifiers": descriptive_relationships(package),
        "rightsList": rights_list,
        "rightsHolders": rights_holders if isinstance(rights_holders, list) else [rights_holders],
        "copyrightYear": str(node_license.get("year") or ""),
        "descriptions": (
            [{"description": description, "descriptionType": "Abstract"}] if description else []
        ),
        "fundingReferences": descriptive_funding(custom_attributes),
        "affiliatedInstitutions": institutions,
        "url": str(project.get("url") or ""),
        "osf": {
            "guid": str(project.get("guid") or ""),
            "rootGuid": str(project.get("root_guid") or hierarchy.get("root_guid") or ""),
            "parentGuid": str(hierarchy.get("parent_guid") or ""),
            "visibility": "Public" if project.get("public") else "Private",
            "category": str(project.get("category") or ""),
            "currentUserPermissions": attributes.get("current_user_permissions") or [],
        },
    }


def render_descriptive_catalog_html(catalog: dict[str, Any]) -> str:
    root = catalog.get("root_project") or {}
    records = catalog.get("records") or []
    title = str(root.get("title") or "OSF project")
    guid = str(root.get("guid") or "unknown")
    contents = "".join(
        f'<li class="depth-{1 if (record.get("osf") or {}).get("parentGuid") else 0}">'
        f'<a href="#record-{html.escape(str((record.get("osf") or {}).get("guid") or ""), quote=True)}">'
        f'{html.escape(str(((record.get("titles") or [{}])[0]).get("title") or "Untitled"))} '
        f'[{html.escape(str((record.get("osf") or {}).get("guid") or ""))}]</a></li>'
        for record in records
    )
    articles: list[str] = []
    for record in records:
        osf = record.get("osf") or {}
        record_guid = str(osf.get("guid") or "")
        record_title = str(((record.get("titles") or [{}])[0]).get("title") or "Untitled")
        rows = [
            ("OSF GUID", record_guid),
            ("URL and identifiers", record.get("identifiers") or []),
            ("Creators", record.get("creators") or []),
            ("Other contributors", record.get("contributors") or []),
            ("Description", record.get("descriptions") or []),
            ("Resource type", record.get("types") or {}),
            ("Subjects and tags", record.get("subjects") or []),
            ("Language", record.get("language") or ""),
            ("License and rights", record.get("rightsList") or []),
            ("Rights holders", record.get("rightsHolders") or []),
            ("Copyright year", record.get("copyrightYear") or ""),
            ("Affiliated institutions", record.get("affiliatedInstitutions") or []),
            ("Funding", record.get("fundingReferences") or []),
            ("Dates", record.get("dates") or []),
            ("Related resources", record.get("relatedIdentifiers") or []),
            ("Visibility", osf.get("visibility") or ""),
            ("Current user permissions", osf.get("currentUserPermissions") or []),
        ]
        table_rows = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{metadata_html_value(value)}</td></tr>"
            for label, value in rows
        )
        articles.append(
            f'<article id="record-{html.escape(record_guid, quote=True)}">'
            f'<div class="record-heading"><div><h2>{html.escape(record_title)}</h2>'
            f'<p>{html.escape(record_guid)} · {html.escape(str(osf.get("category") or "project").replace("_", " ").title())}</p></div>'
            f'<span class="visibility {html.escape(str(osf.get("visibility") or "").casefold())}">{html.escape(str(osf.get("visibility") or ""))}</span></div>'
            f'<table class="catalog-table"><tbody>{table_rows}</tbody></table>'
            '<p class="back"><a href="#contents">Back to contents</a></p></article>'
        )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<title>{html.escape(title)} [{html.escape(guid)}] - Descriptive Metadata Catalog</title>
<style>
:root{{--ink:#243746;--muted:#647783;--line:#d8e2e7;--blue:#1e638f;--bg:#f3f7f9;--paper:#fff;--green:#e7f5eb;--orange:#fff0d8}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
header{{padding:31px max(22px,calc((100% - 1050px)/2));color:#fff;background:linear-gradient(135deg,#243746,#2c6e9f)}}header h1{{margin:0 0 5px;font-size:1.9rem}}header p{{margin:0;opacity:.88}}
main{{width:min(1050px,calc(100% - 30px));margin:20px auto 60px}}nav,article,.about{{margin:0 0 17px;padding:19px;background:var(--paper);border:1px solid var(--line);border-radius:9px;box-shadow:0 3px 13px rgba(20,45,60,.05)}}
h2{{margin:0}}nav h2,.about h2{{margin:0 0 9px}}a{{color:var(--blue);overflow-wrap:anywhere}}nav ol{{margin:0;padding-left:23px}}nav .depth-1{{margin-left:20px}}
.record-heading{{display:flex;justify-content:space-between;align-items:flex-start;gap:15px;margin-bottom:12px}}.record-heading p{{margin:2px 0;color:var(--muted)}}.visibility{{padding:3px 9px;border-radius:999px;font-size:.72rem;font-weight:800}}.visibility.public{{color:#276137;background:var(--green)}}.visibility.private{{color:#814c0b;background:var(--orange)}}
.catalog-table,.metadata-table{{width:100%;border-collapse:collapse}}.catalog-table th,.catalog-table td,.metadata-table th,.metadata-table td{{padding:8px 10px;border:1px solid var(--line);text-align:left;vertical-align:top}}.catalog-table>tbody>tr>th{{width:23%;background:#f5f8fa}}.metadata-table th{{width:29%;background:#f7f9fa;font-size:.83rem}}
.metadata-list{{margin:0;padding-left:22px}}.metadata-list>li{{margin:4px 0}}.empty-value,.about p,footer{{color:var(--muted)}}.text-value{{white-space:pre-wrap;overflow-wrap:anywhere}}.back{{text-align:right;font-size:.78rem}}footer{{text-align:center;font-size:.78rem}}
@media(max-width:680px){{.catalog-table>tbody>tr>th{{width:34%}}.record-heading{{display:block}}.visibility{{display:inline-block;margin-top:7px}}}}
</style></head><body>
<header><h1>{html.escape(title)} [{html.escape(guid)}]</h1><p>Descriptive metadata catalog · project and component records</p></header>
<main>
<section class="about"><h2>About this catalog</h2><p>This is a DataCite-inspired descriptive snapshot assembled from OSF metadata. It is designed for reading and reuse, but it is not represented as a validated DOI deposit record.</p><p>Exported {html.escape(str(catalog.get("exported_utc") or ""))} · {len(records)} record(s)</p></section>
<nav id="contents"><h2>Contents</h2><ol>{contents}</ol></nav>
{''.join(articles)}
<footer>Generated locally by OSF Export Checklist {html.escape(SCRIPT_VERSION)}. A matching normalized JSON catalog is stored beside this document.</footer>
</main></body></html>'''


def write_descriptive_metadata_catalog(
    root: NodeRecord,
    packages: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[Path, Path]:
    assert root.folder is not None
    catalog = {
        "format": "OSF DataCite-inspired descriptive metadata catalog",
        "format_version": "1.0",
        "based_on": {
            "name": "DataCite Metadata Schema",
            "version": "4.7",
            "conformance": "inspired",
            "note": "This local export is not asserted to be a valid DataCite DOI deposit record.",
        },
        "exported_utc": utc_now(),
        "exporter_version": SCRIPT_VERSION,
        "root_project": {"guid": root.guid, "title": root.title, "url": root.url},
        "record_count": len(packages),
        "records": [descriptive_metadata_record(package) for package in packages],
        "source_warnings": list(warnings),
    }
    stem = f"{root.display_name} - Descriptive Metadata Catalog"
    json_path = root.folder / f"{stem}.json"
    html_path = root.folder / f"{stem}.html"
    write_json_document(json_path, catalog)
    html_path.write_text(render_descriptive_catalog_html(catalog), encoding="utf-8")
    return json_path, html_path


def metadata_source_filename(node: NodeRecord, label: str) -> str:
    return owner_prefixed_name(node, f" - {safe_segment(label, 90)}.json")


def collect_metadata_package(
    client: OSFClient,
    root: NodeRecord,
    node: NodeRecord,
    core_document: dict[str, Any],
    job: ExportJob,
    errors: list[str],
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
        if key not in METADATA_RELATIONSHIPS or not url:
            continue
        check_cancel(job)
        try:
            source = collect_paginated_document(client, url)
            resolved[key] = source
            catalog[key]["status"] = "resolved"
            catalog[key]["record_count"] = source["record_count"]
        except PilotError as error:
            message = f"Could not resolve {key} metadata for {node.display_name}: {error}"
            errors.append(message)
            resolved[key] = {"status": "error", "source_url": url, "error": str(error), "data": []}
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
    except PilotError as error:
        errors.append(f"Could not retrieve custom metadata for {node.display_name}: {error}")
        custom = {"status": "error", "source_url": custom_url, "error": str(error), "data": None}

    cedar_url = client.api_url(f"nodes/{node.guid}/cedar_metadata_records/")
    try:
        first_cedar = client.get_optional_json(cedar_url, {404, 405})
        cedar = (
            collect_paginated_document(client, cedar_url, first_cedar)
            if first_cedar is not None
            else {"status": "not_available", "source_url": cedar_url, "record_count": 0, "data": []}
        )
    except PilotError as error:
        errors.append(f"Could not retrieve CEDAR metadata for {node.display_name}: {error}")
        cedar = {"status": "error", "source_url": cedar_url, "error": str(error), "data": []}

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


def export_metadata(
    client: OSFClient,
    root: NodeRecord,
    nodes: list[NodeRecord],
    job: ExportJob,
    errors: list[str],
    report: ProgressCallback,
    start: float,
    span: float,
) -> tuple[int, int]:
    rows: list[dict[str, str]] = []
    resources: list[dict[str, Any]] = []
    node_count = max(1, len(nodes))
    for index, node in enumerate(nodes):
        check_cancel(job)
        report(start + span * index / node_count, f"Metadata: {node.display_name}")
        try:
            document = client.get_json(client.api_url(f"nodes/{node.guid}/"))
            raw = document.get("data")
            if not isinstance(raw, dict):
                raise PilotError("OSF returned an unexpected metadata response")
            node.raw = raw
            assert node.folder is not None
            metadata_folder = node.folder / "Metadata"
            responses_folder = metadata_folder / "API Responses"
            responses_folder.mkdir(exist_ok=True)
            package = collect_metadata_package(client, root, node, document, job, errors)

            write_json_document(
                responses_folder / metadata_source_filename(node, "Core API Response"),
                document,
            )
            write_json_document(
                responses_folder / metadata_source_filename(node, "Custom Metadata"),
                package["custom_item_metadata"],
            )
            write_json_document(
                responses_folder / metadata_source_filename(node, "CEDAR Metadata"),
                package["cedar_metadata_records"],
            )
            for key, source in package["resolved_relationships"].items():
                label = METADATA_RELATIONSHIPS.get(key, humanize_metadata_key(key))
                write_json_document(
                    responses_folder / metadata_source_filename(node, label),
                    source,
                )

            complete_stem = owner_prefixed_name(node, " - Complete Metadata")
            write_json_document(metadata_folder / f"{complete_stem}.json", package)
            (metadata_folder / f"{complete_stem}.html").write_text(
                render_complete_metadata_html(package), encoding="utf-8"
            )
            row = metadata_csv_row(node, raw, root.guid, package)
            rows.append(row)
            resources.append(package)
            node.metadata_count = 1
        except (PilotError, OSError) as error:
            errors.append(f"Could not export metadata for {node.display_name}: {error}")

    assert root.folder is not None
    report(start + span * 0.97, "Writing descriptive metadata catalog")
    try:
        write_descriptive_metadata_catalog(root, resources, errors)
    except (OSError, TypeError, ValueError) as error:
        errors.append(f"Could not write descriptive metadata catalog for {root.display_name}: {error}")

    stem = f"{root.display_name} - Project and Component Metadata"
    fields = [
        "guid",
        "title",
        "url",
        "root_guid",
        "parent_guid",
        "public",
        "category",
        "description",
        "date_created",
        "date_modified",
        "registration",
        "tags",
        "subjects",
        "language",
        "resource_type_general",
        "funders",
        "contributors",
        "affiliated_institutions",
        "identifiers",
        "license",
        "registrations",
        "linked_projects",
        "linked_registrations",
        "preprints",
        "storage_providers",
        "current_user_permissions",
        "api_url",
    ]
    try:
        with (root.folder / f"{stem}.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        combined = {
            "root_project": {"guid": root.guid, "title": root.title, "url": root.url},
            "exported_utc": utc_now(),
            "items": resources,
        }
        with (root.folder / f"{stem}.json").open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(combined, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as error:
        errors.append(f"Could not write combined metadata files for {root.display_name}: {error}")
    report(start + span, "Metadata export finished")
    return len(resources), sum(node.related_metadata_count for node in nodes)


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
    label = version_id.zfill(width) if version_id.isdigit() else safe_segment(version_id, 30)
    created = str((version.get("attributes") or {}).get("date_created") or "date-unknown")
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
    with (history_folder / f"{stem}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
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
    with (history_folder / f"{stem}.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def export_wikis(
    client: OSFClient,
    nodes: list[NodeRecord],
    job: ExportJob,
    errors: list[str],
    report: ProgressCallback,
    start: float,
    span: float,
) -> tuple[int, int]:
    page_total = 0
    version_total = 0
    node_count = max(1, len(nodes))
    for node_index, node in enumerate(nodes):
        check_cancel(job)
        report(start + span * node_index / node_count, f"Wikis: {node.display_name}")
        relationships = node.raw.get("relationships") or {}
        wikis_url = related_href(relationships.get("wikis")) or client.api_url(f"nodes/{node.guid}/wikis/")
        try:
            wikis = list(client.paginate(wikis_url))
        except PilotError as error:
            errors.append(f"Could not list wikis for {node.display_name}: {error}")
            continue
        wikis.sort(key=lambda item: wiki_page_details(item)[0].casefold())

        for wiki in wikis:
            check_cancel(job)
            page_name, wiki_id = wiki_page_details(wiki)
            download_url = link_href((wiki.get("links") or {}).get("download"))
            if not download_url and wiki_id != "unknown":
                download_url = client.api_url(f"wikis/{wiki_id}/content/")
            try:
                if not isinstance(download_url, str) or not download_url:
                    raise PilotError("No current-content download link was provided")
                content = client.get_text(download_url)
                assert node.folder is not None
                (node.folder / "Wikis" / wiki_filename(node, wiki)).write_text(content, encoding="utf-8")
                node.wiki_count += 1
                page_total += 1
            except (PilotError, OSError) as error:
                errors.append(f"Could not save current wiki {wiki_id} for {node.display_name}: {error}")

            versions_url = related_href((wiki.get("relationships") or {}).get("versions"))
            if not versions_url and wiki_id != "unknown":
                versions_url = client.api_url(f"wikis/{wiki_id}/versions/")
            if not versions_url:
                errors.append(f"Wiki {wiki_id} on {node.display_name} had no version-history link.")
                continue
            try:
                versions = sorted(client.paginate(versions_url), key=wiki_version_sort_key)
            except PilotError as error:
                errors.append(f"Could not list versions of wiki {wiki_id} for {node.display_name}: {error}")
                continue
            if not versions:
                continue

            assert node.folder is not None
            history_folder = (
                node.folder
                / "Wikis"
                / "Version History"
                / f"{safe_segment(page_name, 120)} [{wiki_id}]"
            )
            history_folder.mkdir(parents=True, exist_ok=True)
            widths = [len(str(item.get("id"))) for item in versions if str(item.get("id") or "").isdigit()]
            width = max([4, *widths])
            metadata_rows: list[dict[str, Any]] = []
            for version_index, version in enumerate(versions, start=1):
                check_cancel(job)
                version_id = str(version.get("id") or version_index)
                filename = wiki_version_filename(node, wiki, version, width, version_index)
                version_url = link_href((version.get("links") or {}).get("download"))
                if not version_url and wiki_id != "unknown":
                    version_url = client.api_url(f"wikis/{wiki_id}/versions/{version_id}/content/")
                attributes = version.get("attributes") or {}
                row: dict[str, Any] = {
                    "version": version_id,
                    "date_created": str(attributes.get("date_created") or ""),
                    "user_guid": relationship_id((version.get("relationships") or {}).get("user")) or "",
                    "size": attributes.get("size", ""),
                    "content_type": str(attributes.get("content_type") or ""),
                    "filename": filename,
                    "download_status": "failed",
                    "error": "",
                    "api_url": str((version.get("links") or {}).get("self") or ""),
                }
                try:
                    if not isinstance(version_url, str) or not version_url:
                        raise PilotError("No version-content download link was provided")
                    (history_folder / filename).write_text(client.get_text(version_url), encoding="utf-8")
                    row["download_status"] = "downloaded"
                    node.wiki_version_count += 1
                    version_total += 1
                except (PilotError, OSError) as error:
                    row["error"] = str(error)
                    errors.append(
                        f"Could not save wiki {wiki_id} version {version_id} for {node.display_name}: {error}"
                    )
                metadata_rows.append(row)
            try:
                write_wiki_history_metadata(history_folder, node, wiki, metadata_rows)
            except OSError as error:
                errors.append(f"Could not write wiki-history metadata for {wiki_id}: {error}")
    report(start + span, "Wiki export finished")
    return page_total, version_total


def log_origin_guid(log: dict[str, Any]) -> str | None:
    relationships = log.get("relationships") or {}
    original = relationship_id(relationships.get("original_node"))
    if original:
        return original
    params_node = ((log.get("attributes") or {}).get("params") or {}).get("params_node") or {}
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
    errors: list[str],
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
        except PilotError as error:
            errors.append(f"Could not retrieve activity logs for {node.display_name}: {error}")
    report(start + span, "Activity logs retrieved")
    return sorted(
        by_id.values(),
        key=lambda item: str((item.get("attributes") or {}).get("date") or ""),
        reverse=True,
    )


def log_csv_row(log: dict[str, Any], owner_guid: str, owner_title: str) -> dict[str, str]:
    attributes = log.get("attributes") or {}
    relationships = log.get("relationships") or {}
    return {
        "date": str(attributes.get("date") or ""),
        "action": str(attributes.get("action") or ""),
        "log_id": str(log.get("id") or ""),
        "user_guid": relationship_id(relationships.get("user")) or "",
        "node_guid": owner_guid,
        "node_title": owner_title,
        "params_json": json.dumps(attributes.get("params") or {}, ensure_ascii=False, separators=(",", ":")),
        "relationships_json": json.dumps(relationships, ensure_ascii=False, separators=(",", ":")),
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


def organize_logs(
    root: NodeRecord,
    nodes: list[NodeRecord],
    logs: list[dict[str, Any]],
    errors: list[str],
) -> int:
    node_map = {node.guid: node for node in nodes}
    grouped: dict[str, list[dict[str, Any]]] = {node.guid: [] for node in nodes}
    former: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for log in logs:
        origin = log_origin_guid(log)
        if origin in node_map:
            grouped[origin].append(log)
        elif origin:
            parent = log_context_guid(log)
            if parent not in node_map:
                parent = root.guid
            former.setdefault((str(parent), origin), []).append(log)
        else:
            unassigned.append(log)

    for node in nodes:
        assert node.folder is not None
        node.log_count = len(grouped[node.guid])
        try:
            write_log_files(node.folder, node.display_name, node.guid, node.title, grouped[node.guid])
        except OSError as error:
            errors.append(f"Could not write activity logs for {node.display_name}: {error}")
    for (parent_guid, origin_guid), former_logs in sorted(former.items()):
        parent = node_map[parent_guid]
        assert parent.folder is not None
        title = log_origin_title(former_logs[0], origin_guid)
        display = title_guid_name(title, origin_guid)
        folder = parent.folder / "Former or inaccessible components" / display
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_log_files(folder, display, origin_guid, title, former_logs)
        except OSError as error:
            errors.append(f"Could not write activity logs for {display}: {error}")
    if unassigned:
        assert root.folder is not None
        try:
            write_log_files(
                root.folder,
                f"{root.display_name} - Unassigned",
                "",
                "Unassigned activity",
                unassigned,
            )
        except OSError as error:
            errors.append(f"Could not write unassigned activity logs: {error}")
    return len(logs)


def action_title(action: str) -> str:
    return {
        "metadata": "Metadata",
        "wikis": "Wikis",
        "logs": "Activity Logs",
        "everything": "Everything",
    }[action]


def write_export_summary(
    root: NodeRecord,
    nodes: list[NodeRecord],
    job: ExportJob,
    counts: dict[str, int],
    errors: list[str],
) -> Path:
    assert root.folder is not None
    path = root.folder / f"{root.display_name} - {action_title(job.action)} Export Summary.json"
    document = {
        "pilot_version": SCRIPT_VERSION,
        "action": job.action,
        "root_project": {
            "guid": root.guid,
            "title": root.title,
            "url": root.url,
        },
        "zip_requested": job.make_zip,
        "started_utc": job.started_at,
        "finished_utc": utc_now(),
        "counts": {"projects_and_components": len(nodes), **counts, "errors": len(errors)},
        "items": [
            {
                "guid": node.guid,
                "title": node.title,
                "parent_guid": node.parent_guid,
                "relative_folder": str(node.folder.relative_to(root.folder)) if node.folder != root.folder else ".",
                "metadata_records": node.metadata_count,
                "resolved_related_metadata_records": node.related_metadata_count,
                "wiki_pages": node.wiki_count,
                "wiki_versions": node.wiki_version_count,
                "activity_log_entries": node.log_count,
            }
            for node in nodes
        ],
        "errors": errors,
        "notes": [
            "Each Metadata folder contains readable HTML, comprehensive JSON, and normalized API source records.",
            "The root folder contains a single DataCite-inspired descriptive metadata catalog in HTML and JSON, covering the project and all exported components.",
            "Custom item metadata includes language, resource type, and funding when OSF has a record.",
            "Metadata relationship links are resolved for contributors, institutions, identifiers, licenses, registrations, linked resources, citations, regions, and storage providers when available.",
            "View-only links are not retrieved because they can grant access to private content.",
            "The root folder also contains combined project/component metadata in expanded CSV and JSON.",
            "Current wiki pages are at the top of each Wikis folder.",
            "Every available wiki version is under Wikis/Version History.",
            "Project files are not downloaded in this pilot version.",
        ],
    }
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def should_zip(path: Path, action: str) -> bool:
    if path.name.endswith(".part") or path.suffix.casefold() == ".zip":
        return False
    parts = set(path.parts)
    if action == "metadata":
        return (
            "Metadata" in parts
            or "Project and Component Metadata" in path.name
            or "Descriptive Metadata Catalog" in path.name
            or "Metadata Export Summary" in path.name
        )
    if action == "wikis":
        return "Wikis" in parts or "Wikis Export Summary" in path.name
    if action == "logs":
        return "Activity Log" in path.name or "Activity Logs Export Summary" in path.name
    return True


def make_action_zip(
    root: NodeRecord,
    action: str,
    job: ExportJob,
    report: ProgressCallback,
) -> Path:
    assert root.folder is not None
    zip_path = root.folder.parent / f"{root.display_name} - {action_title(action)}.zip"
    temporary = zip_path.with_name(zip_path.name + ".part")
    candidates = [path for path in root.folder.rglob("*") if path.is_file() and should_zip(path, action)]
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for index, path in enumerate(candidates, start=1):
            check_cancel(job)
            archive.write(path, path.relative_to(root.folder.parent))
            report(0.96 + 0.04 * index / max(1, len(candidates)), f"Creating ZIP: {index}/{len(candidates)}")
    os.replace(temporary, zip_path)
    return zip_path


def open_local_folder(path: Path) -> None:
    path = path.expanduser().resolve()
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


class PilotApp:
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
        self.state_path = output_base / f".checklist-state-{safe_segment(account_id, 80)}.json"
        self.checks = self.load_checks()
        self.worker = threading.Thread(target=self.worker_loop, name="osf-export-worker", daemon=True)
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
        document = {"account_id": self.account_id, "checks": self.checks, "updated_utc": utc_now()}
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def set_check(self, guid: str, checked: bool) -> None:
        with self.lock:
            if checked:
                self.checks[guid] = True
            else:
                self.checks.pop(guid, None)
            self.save_checks()

    def clear_checks(self) -> None:
        with self.lock:
            self.checks = {}
            self.save_checks()

    def queue_job(self, root_guid: str, action: str, make_zip: bool) -> ExportJob:
        if root_guid not in self.root_map:
            raise PilotError(f"Project {root_guid} is not in this pilot checklist.")
        if action not in VALID_ACTIONS:
            raise PilotError(f"Unknown export action: {action}")
        root = self.root_map[root_guid]
        job = ExportJob(
            id=secrets.token_hex(8),
            root_guid=root_guid,
            root_title=root.title,
            action=action,
            make_zip=make_zip,
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
                raise PilotError("The requested job was not found.")
            return self.queue_job(
                previous.root_guid,
                previous.action,
                previous.make_zip,
            )

    def cancel_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise PilotError("The requested job was not found.")
            if job.status in {"queued", "running"}:
                job.cancel_requested = True
                job.message = "Cancellation requested"

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "checks": dict(self.checks),
                "jobs": [self.jobs[job_id].public_dict() for job_id in self.job_order if job_id in self.jobs],
                "output_base": str(self.output_base),
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
        errors: list[str] = []
        counts = {
            "metadata_records": 0,
            "resolved_related_metadata_records": 0,
            "wiki_pages": 0,
            "wiki_versions": 0,
            "unique_activity_log_entries": 0,
        }
        try:
            root = discover_tree(self.client, job.root_guid)
            nodes = flatten_tree(root)
            root_folder = self.output_base / root.display_name
            ensure_hierarchy(root, root_folder, job.action)
            job.output_folder = str(root_folder)
            callback = lambda progress, message: self.report(job, progress, message)

            if job.action == "everything":
                metadata_records, related_records = export_metadata(
                    self.client, root, nodes, job, errors, callback, 0.03, 0.17
                )
                counts["metadata_records"] = metadata_records
                counts["resolved_related_metadata_records"] = related_records
                pages, versions = export_wikis(
                    self.client, nodes, job, errors, callback, 0.20, 0.39
                )
                counts["wiki_pages"] = pages
                counts["wiki_versions"] = versions
                logs = collect_logs(self.client, nodes, job, errors, callback, 0.59, 0.27)
                counts["unique_activity_log_entries"] = organize_logs(root, nodes, logs, errors)
                self.report(job, 0.94, "Metadata, wikis, and activity logs finished")
            elif job.action == "metadata":
                metadata_records, related_records = export_metadata(
                    self.client, root, nodes, job, errors, callback, 0.03, 0.91
                )
                counts["metadata_records"] = metadata_records
                counts["resolved_related_metadata_records"] = related_records
            elif job.action == "wikis":
                pages, versions = export_wikis(
                    self.client, nodes, job, errors, callback, 0.03, 0.91
                )
                counts["wiki_pages"] = pages
                counts["wiki_versions"] = versions
            elif job.action == "logs":
                logs = collect_logs(self.client, nodes, job, errors, callback, 0.03, 0.76)
                self.report(job, 0.84, "Organizing activity logs")
                counts["unique_activity_log_entries"] = organize_logs(root, nodes, logs, errors)
                self.report(job, 0.94, "Activity-log export finished")
            check_cancel(job)
            self.report(job, 0.95, "Writing export summary")
            write_export_summary(root, nodes, job, counts, errors)
            if job.make_zip:
                zip_path = make_action_zip(root, job.action, job, callback)
                job.zip_path = str(zip_path)
            self.report(job, 1.0, "Export completed" if not errors else "Export completed with warnings")
            with self.lock:
                job.errors = errors
                job.status = "completed" if not errors else "completed_with_warnings"
                job.finished_at = utc_now()
        except JobCancelled:
            with self.lock:
                job.status = "cancelled"
                job.message = "Cancelled. Completed outputs are retained and a retry can continue."
                job.finished_at = utc_now()
                job.errors = errors
        except Exception as error:
            with self.lock:
                job.status = "failed"
                job.message = str(error)
                job.finished_at = utc_now()
                job.errors = [*errors, str(error)]


def flatten_for_csv(nodes: list[NodeRecord], depth: int = 0, parent_guid: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in nodes:
        rows.append(
            {
                "guid": node.guid,
                "title": node.title,
                "url": node.url,
                "visibility": node.visibility,
                "depth": depth,
                "parent_guid": parent_guid,
            }
        )
        rows.extend(flatten_for_csv(node.children, depth + 1, node.guid))
    return rows


def render_node(node: NodeRecord, checked: bool, depth: int = 0, root: bool = False) -> str:
    title = html.escape(node.title)
    guid = html.escape(node.guid)
    url = html.escape(node.url, quote=True)
    category = html.escape(node.category.replace("_", " ").title())
    modified = html.escape(node.date_modified)
    visibility = node.visibility
    visibility_class = visibility.casefold()
    search = html.escape(
        f"{node.title} {node.guid} {node.category} {visibility}".casefold(), quote=True
    )
    children_html = "".join(render_node(child, False, depth + 1, False) for child in node.children)
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
                ("wikis", "Wikis + history"),
                ("logs", "Activity logs"),
                ("everything", "Everything"),
            )
        )
        actions = f'<div class="row-actions">{buttons}</div>'
    checked_attr = " checked" if checked else ""
    checked_class = " checked" if checked else ""
    metadata = f"{guid}{f' · {category}' if category else ''}{f' · Modified {modified}' if modified else ''}"
    child_block = f'<ul class="children">{children_html}</ul>' if node.children else ""
    return f"""
<li class="node{checked_class}" data-guid="{guid}" data-public="{str(node.public).lower()}"
    data-search="{search}" data-depth="{depth}" data-root="{str(root).lower()}">
  <div class="node-row">
    {toggle}
    <input class="node-check" type="checkbox" aria-label="Mark reviewed"{checked_attr}>
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


def render_html(app: PilotApp) -> str:
    all_nodes = [node for root in app.roots for node in flatten_tree(root)]
    rows = flatten_for_csv(app.roots)
    public_count = sum(1 for node in all_nodes if node.public)
    private_count = len(all_nodes) - public_count
    nodes_html = "".join(render_node(root, bool(app.checks.get(root.guid)), root=True) for root in app.roots)
    # Component check states are applied in JavaScript from this complete map.
    replacements = {
        "__ACCOUNT__": html.escape(app.account_name),
        "__ROOT_COUNT__": f"{len(app.roots):,}",
        "__NODE_COUNT__": f"{len(all_nodes):,}",
        "__PUBLIC_COUNT__": f"{public_count:,}",
        "__PRIVATE_COUNT__": f"{private_count:,}",
        "__NODE_HTML__": nodes_html,
        "__SESSION_KEY__": json.dumps(app.session_key),
        "__ROWS_JSON__": json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"),
        "__CHECKS_JSON__": json.dumps(app.checks, ensure_ascii=False).replace("</", "<\\/"),
        "__OUTPUT_BASE__": html.escape(str(app.output_base)),
        "__VERSION__": SCRIPT_VERSION,
    }
    template = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<title>OSF Export Checklist - Comprehensive Metadata</title>
<style>
:root{--ink:#20313d;--muted:#657985;--line:#dbe4e9;--blue:#2c6e9f;--blue2:#194d72;--paper:#fff;--bg:#f3f7f9;--green:#e8f5eb;--orange:#fff1dc;--red:#a33b31;--purple:#6b4f8b}
*{box-sizing:border-box} body{margin:0;color:var(--ink);background:var(--bg);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
header{padding:27px max(20px,calc((100% - 1240px)/2));color:#fff;background:linear-gradient(135deg,#243746,#2c6e9f)}
header h1{margin:0 0 3px;font-size:2rem} header p{margin:0;opacity:.87}
main{width:min(1240px,calc(100% - 32px));margin:0 auto;padding:20px 0 60px}
.notice{padding:13px 15px;margin-bottom:15px;background:#fff5d8;border:1px solid #e4ca75;border-radius:8px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-bottom:15px}
.stats div,.toolbar,.batch-panel,.panel,.jobs{background:var(--paper);border:1px solid var(--line);border-radius:9px;box-shadow:0 4px 16px rgba(30,50,60,.06)}
.stats div{padding:14px}.stats strong{display:block;font-size:1.65rem}.stats span,.muted{color:var(--muted);font-size:.83rem}
.toolbar{position:sticky;top:8px;z-index:10;display:flex;flex-wrap:wrap;gap:8px;padding:12px;margin-bottom:15px}
.toolbar input[type=search]{flex:1 1 260px}.toolbar input,.toolbar select,.toolbar button,.batch-panel button,.action-button,.job button{padding:8px 10px;background:#fff;border:1px solid #b7c5cc;border-radius:5px;font:inherit}
button{cursor:pointer}button.primary{color:#fff;background:var(--blue);border-color:var(--blue)}button.danger{color:var(--red);border-color:#d6a9a4}
.progress{flex:1 1 100%;display:flex;align-items:center;gap:12px;color:var(--muted);font-size:.8rem}.progress i,.job-progress{height:9px;flex:1;overflow:hidden;background:#e6edf0;border-radius:99px}.progress b,.job-progress b{display:block;width:0;height:100%;background:var(--blue)}
.batch-panel,.panel,.jobs{padding:17px;margin-bottom:15px}.batch-panel h2,.panel h2,.jobs h2{margin:0 0 10px}.batch-buttons{display:flex;flex-wrap:wrap;gap:8px;margin:11px 0}.options{display:flex;flex-wrap:wrap;gap:18px;color:var(--muted);font-size:.88rem}.options label,.batch-label{display:inline-flex;align-items:center;gap:5px}
.tree,.children{padding:0;margin:0;list-style:none}.children{margin-left:31px;border-left:2px solid #e0e8ec}
.node-row{display:flex;align-items:flex-start;gap:9px;min-height:60px;padding:9px;border-bottom:1px solid #edf1f3}.node-row:hover{background:#f7fafb}.toggle{width:24px;padding:0;background:transparent;border:0;cursor:pointer;font-size:1rem}.toggle-space{width:24px}.node-check{width:18px;height:18px;margin-top:3px}.node-info{min-width:0;flex:1}.title-line{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.node-info a{color:var(--blue2);font-weight:700;text-decoration:none;overflow-wrap:anywhere}.node-info a:hover{text-decoration:underline}.node-info small{color:var(--muted)}
.badge{padding:2px 7px;border-radius:999px;font-size:.68rem;font-weight:800}.badge.public{color:#276137;background:var(--green)}.badge.private{color:#814c0b;background:var(--orange)}.batch-label{font-size:.76rem;color:var(--purple);font-weight:650}
.row-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.action-button{padding:5px 8px;font-size:.78rem;color:var(--blue2)}.action-button[data-action=everything]{color:#fff;background:var(--blue);border-color:var(--blue)}
.node.checked>.node-row{background:#f0f7f2}.node.checked>.node-row .node-info>a{color:var(--muted);text-decoration:line-through}.node.collapsed>.children{display:none}.node.collapsed>.node-row .toggle{transform:rotate(-90deg)}.node.hidden{display:none}
.job{padding:12px 0;border-top:1px solid var(--line)}.job:first-of-type{border-top:0}.job-head,.job-foot{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px}.job-title{font-weight:750}.job-status{padding:2px 7px;border-radius:999px;background:#edf2f5;font-size:.72rem;font-weight:800}.job-status.completed{color:#276137;background:var(--green)}.job-status.failed,.job-status.cancelled{color:var(--red);background:#f9e5e3}.job-status.completed_with_warnings{color:#814c0b;background:var(--orange)}.job-progress{margin:8px 0}.job-message{color:var(--muted);font-size:.82rem}.job-errors{margin:7px 0;color:var(--red);font-size:.78rem}.empty{color:var(--muted);font-style:italic}
footer{color:var(--muted);font-size:.8rem;text-align:center}@media(max-width:760px){.stats{grid-template-columns:1fr 1fr}.children{margin-left:13px}.toolbar{position:static}}
</style>
</head>
<body>
<header><h1>OSF Export Checklist</h1><p>__ACCOUNT__ · comprehensive metadata edition · <strong>__VERSION__</strong></p></header>
<main>
  <div class="notice"><strong>Local and private:</strong> the OSF token remains in the running Python process and is not placed in this page. Keep the terminal window open while exports run. The page contains private project titles and structure.</div>
  <section class="stats"><div><strong>__ROOT_COUNT__</strong><span>Top-level export trees</span></div><div><strong>__NODE_COUNT__</strong><span>Projects and components</span></div><div><strong>__PUBLIC_COUNT__</strong><span>Public</span></div><div><strong>__PRIVATE_COUNT__</strong><span>Private</span></div></section>
  <section class="toolbar">
    <input id="search" type="search" placeholder="Search titles, GUIDs, or categories">
    <select id="visibility"><option value="all">Public and private</option><option value="public">Public only</option><option value="private">Private only</option></select>
    <select id="completion"><option value="all">Reviewed and unreviewed</option><option value="unchecked">Unreviewed only</option><option value="checked">Reviewed only</option></select>
    <button id="expand" type="button">Expand all</button><button id="collapse" type="button">Collapse all</button><button id="clear" type="button">Clear review checks</button><button id="csv" type="button">Export checklist CSV</button>
    <div class="progress"><span id="progressText">0 reviewed</span><i><b id="progressFill"></b></i></div>
  </section>
  <section class="batch-panel">
    <h2>Batch export</h2><p class="muted">Select “Include in batch” on one or more top-level projects, then choose an export.</p>
    <div class="batch-buttons"><button id="selectAllBatch" type="button">Select all projects</button><button id="clearBatch" type="button">Clear batch selection</button></div>
    <div class="batch-buttons"><button data-batch="metadata">Selected: comprehensive metadata</button><button data-batch="wikis">Selected: wikis + history</button><button data-batch="logs">Selected: activity logs</button><button class="primary" data-batch="everything">Selected: everything</button></div>
    <div class="options"><label><input id="zip" type="checkbox"> Create a ZIP after export</label><span>Metadata includes a single DataCite-inspired project/component catalog, readable per-item HTML, comprehensive JSON, custom funding/resource metadata, CEDAR records, and resolved relationship records. “Everything” also includes complete wiki history and activity logs. Project files remain excluded.</span></div>
  </section>
  <section class="jobs"><h2>Export activity</h2><div id="jobs"><div class="empty">No exports queued yet.</div></div></section>
  <section class="panel"><h2>Projects</h2><ul class="tree">__NODE_HTML__</ul></section>
  <footer>Exports are written beneath <strong>__OUTPUT_BASE__</strong>. Review checks are saved locally outside the browser; the OSF token is not saved.</footer>
</main>
<script>
(()=>{"use strict";
const KEY=__SESSION_KEY__,rows=__ROWS_JSON__;let checks=__CHECKS_JSON__;
const nodes=[...document.querySelectorAll('.node')],nodeChecks=[...document.querySelectorAll('.node-check')];
const search=document.getElementById('search'),visibility=document.getElementById('visibility'),completion=document.getElementById('completion');
async function api(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-OSF-Pilot-Key':KEY},body:JSON.stringify(body||{})});const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);return data}
function ownCheck(node){return node.querySelector(':scope > .node-row > .node-check')}
function paint(check){check.closest('.node').classList.toggle('checked',check.checked)}
function progress(){const count=nodeChecks.filter(item=>item.checked).length,total=nodeChecks.length;document.getElementById('progressText').textContent=`${count.toLocaleString()} of ${total.toLocaleString()} reviewed`;document.getElementById('progressFill').style.width=`${total?count/total*100:0}%`}
function filters(){const query=search.value.trim().toLowerCase();function evaluate(node){const children=node.querySelector(':scope > .children'),childVisible=children?[...children.children].map(evaluate).some(Boolean):false,check=ownCheck(node),kind=node.dataset.public==='true'?'public':'private';const own=(!query||node.dataset.search.includes(query))&&(visibility.value==='all'||visibility.value===kind)&&(completion.value==='all'||completion.value===(check.checked?'checked':'unchecked'));const show=own||childVisible;node.classList.toggle('hidden',!show);if(childVisible&&query)node.classList.remove('collapsed');return show}document.querySelectorAll('.tree').forEach(tree=>[...tree.children].forEach(evaluate))}
nodeChecks.forEach(check=>{const guid=check.closest('.node').dataset.guid;check.checked=Boolean(checks[guid]);paint(check);check.addEventListener('change',async()=>{paint(check);checks[guid]=check.checked;if(!check.checked)delete checks[guid];progress();filters();try{await api('/api/checks',{guid,checked:check.checked})}catch(error){alert(error.message)}})});
document.addEventListener('click',event=>{const toggle=event.target.closest('.toggle');if(toggle)toggle.closest('.node').classList.toggle('collapsed')});
search.addEventListener('input',filters);visibility.addEventListener('change',filters);completion.addEventListener('change',filters);
document.getElementById('expand').onclick=()=>nodes.forEach(node=>node.classList.remove('collapsed'));
document.getElementById('collapse').onclick=()=>nodes.forEach(node=>{if(node.querySelector(':scope > .children'))node.classList.add('collapsed')});
document.getElementById('clear').onclick=async()=>{if(!confirm('Clear every saved review checkbox?'))return;nodeChecks.forEach(check=>{check.checked=false;paint(check)});checks={};progress();filters();try{await api('/api/checks/clear',{})}catch(error){alert(error.message)}};
function options(){return{make_zip:document.getElementById('zip').checked}}
async function queueJobs(guids,action){if(!guids.length){alert('Select at least one project for the batch export.');return}try{await api('/api/jobs',{guids,action,...options()});await poll()}catch(error){alert(error.message)}}
document.querySelectorAll('.action-button').forEach(button=>button.onclick=()=>queueJobs([button.dataset.guid],button.dataset.action));
document.querySelectorAll('[data-batch]').forEach(button=>button.onclick=()=>queueJobs([...document.querySelectorAll('.node[data-root=true]')].filter(node=>node.querySelector(':scope > .node-row .batch-check').checked).map(node=>node.dataset.guid),button.dataset.batch));
document.getElementById('selectAllBatch').onclick=()=>document.querySelectorAll('.node[data-root=true] .batch-check').forEach(check=>check.checked=true);
document.getElementById('clearBatch').onclick=()=>document.querySelectorAll('.node[data-root=true] .batch-check').forEach(check=>check.checked=false);
function escapeText(value){const div=document.createElement('div');div.textContent=value??'';return div.innerHTML}
function renderJobs(jobs){const area=document.getElementById('jobs'),openErrors=new Set([...area.querySelectorAll('.job-errors[open]')].map(details=>details.dataset.job));if(!jobs.length){area.innerHTML='<div class="empty">No exports queued yet.</div>';return}area.innerHTML=jobs.map(job=>{const active=['queued','running'].includes(job.status),retry=['failed','cancelled','completed_with_warnings'].includes(job.status),errors=job.errors?.length?`<details class="job-errors" data-job="${escapeText(job.id)}"${openErrors.has(job.id)?' open':''}><summary>${job.errors.length} warning/error(s)</summary><ul>${job.errors.slice(0,20).map(error=>`<li>${escapeText(error)}</li>`).join('')}</ul></details>`:'';return `<div class="job"><div class="job-head"><span class="job-title">${escapeText(job.root_title)} [${escapeText(job.root_guid)}] · ${escapeText(job.action)}</span><span class="job-status ${escapeText(job.status)}">${escapeText(job.status.replaceAll('_',' '))}</span></div><div class="job-progress"><b style="width:${Math.round(job.progress*100)}%"></b></div><div class="job-foot"><span class="job-message">${escapeText(job.message)} · ${Math.round(job.progress*100)}%</span><span>${active?`<button data-cancel="${job.id}">Cancel</button>`:''}${retry?`<button data-retry="${job.id}">Retry</button>`:''}${job.output_folder?`<button data-open="${job.id}">Open folder</button>`:''}</span></div>${errors}</div>`}).join('');area.querySelectorAll('[data-cancel]').forEach(button=>button.onclick=()=>api(`/api/jobs/${button.dataset.cancel}/cancel`,{}).then(poll).catch(error=>alert(error.message)));area.querySelectorAll('[data-retry]').forEach(button=>button.onclick=()=>api(`/api/jobs/${button.dataset.retry}/retry`,{}).then(poll).catch(error=>alert(error.message)));area.querySelectorAll('[data-open]').forEach(button=>button.onclick=()=>api('/api/open-folder',{job_id:button.dataset.open}).catch(error=>alert(error.message)))}
async function poll(){try{const response=await fetch('/api/state',{cache:'no-store'}),data=await response.json();renderJobs(data.jobs||[])}catch(_){}setTimeout(poll,1500)}
function csvEscape(value){const text=String(value??'');return /[",\n]/.test(text)?'"'+text.replaceAll('"','""')+'"':text}
document.getElementById('csv').onclick=()=>{const output=[['guid','title','url','visibility','depth','parent_guid','reviewed']];rows.forEach(row=>output.push([row.guid,row.title,row.url,row.visibility,row.depth,row.parent_guid,checks[row.guid]?'Yes':'No']));const blob=new Blob([output.map(row=>row.map(csvEscape).join(',')).join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='OSF_Project_Checklist_Pilot.csv';document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url)};
progress();filters();poll();
})();
</script>
</body></html>'''
    # Replace executable/data placeholders before inserting user-supplied HTML.
    # This prevents a project title that resembles a marker from being treated
    # as a second template placeholder.
    user_content_markers = {"__ACCOUNT__", "__NODE_HTML__", "__OUTPUT_BASE__"}
    for marker, value in replacements.items():
        if marker not in user_content_markers:
            template = template.replace(marker, value)
    for marker in ("__ACCOUNT__", "__NODE_HTML__", "__OUTPUT_BASE__"):
        value = replacements[marker]
        template = template.replace(marker, value)
    return template


class PilotHandler(BaseHTTPRequestHandler):
    app: PilotApp

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
            self.send_bytes(200, render_html(self.app).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(200, self.app.public_state())
        elif path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
        else:
            self.send_json(404, {"error": "Not found"})

    def authorized(self) -> bool:
        supplied = self.headers.get("X-OSF-Pilot-Key", "")
        return secrets.compare_digest(supplied, self.app.session_key)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise PilotError("Invalid request length") from error
        if length < 0 or length > 1024 * 1024:
            raise PilotError("Request body is too large")
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PilotError("Request body must be JSON") from error
        if not isinstance(document, dict):
            raise PilotError("Request body must be a JSON object")
        return document

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            self.send_json(403, {"error": "This local-control request was not authorized."})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/jobs":
                guids = body.get("guids")
                action = str(body.get("action") or "")
                if not isinstance(guids, list) or not guids:
                    raise PilotError("Choose at least one project.")
                normalized_guids = list(dict.fromkeys(str(guid).lower() for guid in guids))
                if len(normalized_guids) > len(self.app.root_map):
                    raise PilotError("The batch contains more projects than this checklist.")
                missing = [guid for guid in normalized_guids if guid not in self.app.root_map]
                if missing:
                    raise PilotError(f"Project is not in this pilot checklist: {missing[0]}")
                jobs = [
                    self.app.queue_job(
                        guid,
                        action,
                        bool(body.get("make_zip")),
                    )
                    for guid in normalized_guids
                ]
                self.send_json(202, {"jobs": [job.public_dict() for job in jobs]})
                return
            if path == "/api/checks":
                guid = str(body.get("guid") or "").lower()
                if not guid:
                    raise PilotError("A checklist GUID is required.")
                self.app.set_check(guid, bool(body.get("checked")))
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
                    folder = Path(job.output_folder) if job and job.output_folder else self.app.output_base
                resolved = folder.expanduser().resolve()
                if not resolved.is_relative_to(self.app.output_base.resolve()):
                    raise PilotError("The requested folder is outside the export location.")
                open_local_folder(resolved)
                self.send_json(200, {"ok": True})
                return
            self.send_json(404, {"error": "Not found"})
        except PilotError as error:
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
    print("It will not be displayed, placed in the checklist, or saved by this pilot.")
    token = getpass.getpass("OSF token: ").strip()
    if not token:
        raise PilotError("No OSF token was provided.")
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a local OSF checklist with comprehensive metadata, wiki-history, and activity-log export buttons."
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
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("--test", action="store_true", help="Use the OSF test environment")
    parser.add_argument("--no-open", action="store_true", help="Do not open the checklist automatically")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION} (comprehensive metadata + wikis + activity logs; project files disabled)",
    )
    return parser.parse_args(argv)


def choose_pilot_roots(
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
    candidates.sort(key=lambda item: (item.date_modified, item.title.casefold()), reverse=True)
    return candidates[:limit] if limit is not None else candidates


def start_server(app: PilotApp, requested_port: int) -> tuple[ThreadingHTTPServer, int]:
    PilotHandler.app = app
    try:
        server = ThreadingHTTPServer(("127.0.0.1", requested_port), PilotHandler)
    except OSError:
        if requested_port == 0:
            raise
        print(f"Port {requested_port} is busy; selecting another local port.", flush=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), PilotHandler)
    server.daemon_threads = True
    return server, int(server.server_address[1])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"OSF Export Checklist {SCRIPT_VERSION}", flush=True)
    print("Mode: ALL ACCESSIBLE PROJECTS + COMPREHENSIVE METADATA + WIKIS + ACTIVITY LOGS", flush=True)
    print("Project file downloads: DISABLED", flush=True)
    if args.limit is not None and args.limit < 1:
        raise PilotError("--limit must be at least 1.")
    if not 0 <= args.port <= 65535:
        raise PilotError("--port must be between 0 and 65535.")
    token = get_token(args.token)
    explicit_test_url = any(
        urllib.parse.urlparse(value if "://" in value else "").hostname == "test.osf.io"
        for value in args.project
    )
    api_base = TEST_API if args.test or explicit_test_url else PRODUCTION_API
    client = OSFClient(api_base, token)
    print("Checking OSF account...", flush=True)
    account_name, account_id = get_account(client)
    print(f"Authenticated as: {account_name}", flush=True)
    roots = choose_pilot_roots(client, args.project, args.limit)
    if not roots:
        raise PilotError("OSF returned no accessible projects for this pilot.")
    output_base = (args.output or default_output_base()).expanduser().resolve()
    app = PilotApp(client, roots, account_name, account_id, output_base)
    server, port = start_server(app, args.port)
    url = f"http://127.0.0.1:{port}/"
    print()
    print(f"Top-level export trees: {len(roots)}", flush=True)
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
    except PilotError as error:
        print(f"\nError: {error}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as error:
        print(f"\nFile error: {error}", file=sys.stderr)
        raise SystemExit(1)
