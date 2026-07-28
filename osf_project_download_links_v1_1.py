#!/usr/bin/env python3
"""Create a CSV inventory of OSF projects, components, and download links.

The script performs two phases:

1. Retrieve the authenticated user's complete project/component inventory.
2. Inspect every node to determine the required activity-log, wiki, and
   storage-provider link columns before writing the CSV.

Only Python's standard library is required. The OSF Personal Access Token is
kept in memory and is never written to the output.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = "1.1"
PRODUCTION_API = "https://api.osf.io/v2"
PRODUCTION_WEB = "https://osf.io"
TEST_API = "https://api.test.osf.io/v2"
TEST_WEB = "https://test.osf.io"
PAGE_SIZE = 100
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
USER_AGENT = f"OSF-Project-Download-Link-Inventory/{SCRIPT_VERSION}"

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
    "swift": "OpenStack Swift",
    "zotero": "Zotero",
}
PERMISSION_PRECEDENCE = ("admin", "write", "read")


class ExportError(RuntimeError):
    """Raised when the inventory cannot be created safely."""


@dataclass
class Node:
    guid: str
    title: str
    url: str
    public: bool
    permission: str
    parent_guid: str | None
    source_index: int
    depth: int = 0
    children: list["Node"] = field(default_factory=list)


@dataclass
class NodeLinks:
    activity_log_urls: list[str] = field(default_factory=list)
    wiki_urls: list[str] = field(default_factory=list)
    provider_urls: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class OSFClient:
    """Small JSON client with bounded retries for transient OSF failures."""

    def __init__(self, api_base: str, token: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token

    def api_url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.api+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": USER_AGENT,
            },
        )
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    document = json.load(response)
                if not isinstance(document, dict):
                    raise ExportError(f"OSF returned a non-object JSON response for {url}")
                return document
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in RETRYABLE_STATUS_CODES or attempt == MAX_ATTEMPTS:
                    detail = error.read().decode("utf-8", errors="replace")[:500]
                    raise ExportError(
                        f"HTTP {error.code} for {url}"
                        + (f": {detail}" if detail else "")
                    ) from error
                time.sleep(retry_delay(error, attempt))
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt == MAX_ATTEMPTS:
                    break
                time.sleep(min(20.0, 2 ** (attempt - 1)))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ExportError(f"OSF returned invalid JSON for {url}") from error
        raise ExportError(f"Could not reach OSF for {url}: {last_error}")

    def get_all(self, url: str) -> list[dict[str, Any]]:
        """Follow JSON:API pagination and return every object in data."""
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        next_url: str | None = url
        while next_url:
            if next_url in seen:
                raise ExportError(f"OSF repeated a pagination URL: {next_url}")
            seen.add(next_url)
            document = self.get_json(next_url)
            data = document.get("data")
            if not isinstance(data, list):
                raise ExportError(f"Expected a list response from {next_url}")
            records.extend(item for item in data if isinstance(item, dict))
            next_url = link_url((document.get("links") or {}).get("next"))
        return records


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return min(30.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(20.0, 2 ** (attempt - 1))


def link_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        href = value.get("href")
        return str(href) if href else None
    return None


def relationship_id(record: dict[str, Any], name: str) -> str | None:
    relationship = (record.get("relationships") or {}).get(name) or {}
    data = relationship.get("data")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"]).lower()
    return None


def page_size_url(url: str, page_size: int = PAGE_SIZE) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "page[size]"]
    query.append(("page[size]", str(page_size)))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def account_name(client: OSFClient) -> str:
    document = client.get_json(client.api_url("users/me/"))
    attributes = ((document.get("data") or {}).get("attributes") or {})
    return str(attributes.get("full_name") or "OSF user")


def effective_permission(values: Any) -> str:
    """Return the requesting user's highest OSF permission for a node."""
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


def load_inventory(client: OSFClient, limit: int | None = None) -> list[Node]:
    url = page_size_url(client.api_url("users/me/nodes/"))
    records = client.get_all(url)
    if limit is not None:
        records = records[:limit]

    nodes: list[Node] = []
    for index, record in enumerate(records):
        guid = str(record.get("id") or "").lower()
        if not guid:
            continue
        attributes = record.get("attributes") or {}
        links = record.get("links") or {}
        nodes.append(
            Node(
                guid=guid,
                title=str(attributes.get("title") or "Untitled"),
                url=str(links.get("html") or f"https://osf.io/{guid}/"),
                public=bool(attributes.get("public")),
                permission=effective_permission(
                    attributes.get("current_user_permissions")
                ),
                parent_guid=relationship_id(record, "parent"),
                source_index=index,
            )
        )
    return order_hierarchy(nodes)


def order_hierarchy(nodes: list[Node]) -> list[Node]:
    """Compute depths and return roots followed by their components."""
    by_guid = {node.guid: node for node in nodes}
    roots: list[Node] = []
    for node in nodes:
        parent = by_guid.get(node.parent_guid or "")
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    for node in nodes:
        node.children.sort(key=lambda child: child.source_index)
    roots.sort(key=lambda node: node.source_index)

    ordered: list[Node] = []
    visiting: set[str] = set()

    def visit(node: Node, depth: int) -> None:
        if node.guid in visiting:
            raise ExportError(f"Cycle detected in the project hierarchy at {node.guid}")
        visiting.add(node.guid)
        node.depth = depth
        ordered.append(node)
        for child in node.children:
            visit(child, depth + 1)
        visiting.remove(node.guid)

    for root in roots:
        visit(root, 0)
    return ordered


def response_total(document: dict[str, Any]) -> int:
    meta = document.get("meta") or {}
    links_meta = (document.get("links") or {}).get("meta") or {}
    for source in (meta, links_meta):
        value = source.get("total") if isinstance(source, dict) else None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    data = document.get("data")
    return len(data) if isinstance(data, list) else 0


def activity_page_urls(client: OSFClient, guid: str) -> list[str]:
    base = client.api_url(f"nodes/{guid}/logs/")
    first_url = (
        f"{base}?format=jsonapi&page%5Bsize%5D={PAGE_SIZE}&page=1"
    )
    document = client.get_json(first_url)
    total = response_total(document)
    page_count = max(1, math.ceil(total / PAGE_SIZE))
    return [
        f"{base}?format=jsonapi&page%5Bsize%5D={PAGE_SIZE}&page={page}"
        for page in range(1, page_count + 1)
    ]


def wiki_urls(client: OSFClient, guid: str) -> list[str]:
    records = client.get_all(page_size_url(client.api_url(f"nodes/{guid}/wikis/")))
    wikis: list[tuple[str, str]] = []
    for record in records:
        wiki_id = str(record.get("id") or "")
        if not wiki_id:
            continue
        name = str((record.get("attributes") or {}).get("name") or "")
        download = str((record.get("links") or {}).get("download") or "")
        content_url = download or client.api_url(f"wikis/{wiki_id}/content/")
        wikis.append((name.casefold(), content_url))
    return [url for _, url in sorted(wikis)]


def provider_label(provider: str) -> str:
    if provider in PROVIDER_LABELS:
        return PROVIDER_LABELS[provider]
    words = re.sub(r"[_-]+", " ", provider).strip()
    return words.title() if words else "Storage Provider"


def zip_url(link: str) -> str:
    separator = "&" if "?" in link else "?"
    return f"{link}{separator}zip="


def provider_zip_urls(client: OSFClient, guid: str) -> dict[str, str]:
    records = client.get_all(page_size_url(client.api_url(f"nodes/{guid}/files/")))
    output: dict[str, str] = {}
    for record in records:
        attributes = record.get("attributes") or {}
        provider = str(attributes.get("provider") or attributes.get("name") or "").lower()
        upload_link = str((record.get("links") or {}).get("upload") or "")
        if provider:
            output[provider] = zip_url(upload_link) if upload_link else ""
    return output


def inspect_node(client: OSFClient, node: Node) -> NodeLinks:
    result = NodeLinks()
    inspections = (
        ("activity logs", lambda: activity_page_urls(client, node.guid), "activity_log_urls"),
        ("wikis", lambda: wiki_urls(client, node.guid), "wiki_urls"),
        ("storage providers", lambda: provider_zip_urls(client, node.guid), "provider_urls"),
    )
    for label, operation, attribute in inspections:
        try:
            setattr(result, attribute, operation())
        except ExportError as error:
            result.warnings.append(f"{label}: {error}")
    return result


def inspect_all(
    client: OSFClient,
    nodes: list[Node],
    workers: int,
) -> dict[str, NodeLinks]:
    results: dict[str, NodeLinks] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(inspect_node, client, node): node for node in nodes}
        completed = 0
        for future in as_completed(futures):
            node = futures[future]
            completed += 1
            try:
                results[node.guid] = future.result()
            except Exception as error:
                results[node.guid] = NodeLinks(warnings=[f"unexpected inspection error: {error}"])
            print(
                f"Reviewed {completed:,} of {len(nodes):,} projects/components",
                end="\r",
                flush=True,
            )
    print(" " * 78, end="\r", flush=True)
    return results


def dynamic_headers(results: Iterable[NodeLinks]) -> tuple[int, int, list[str], bool]:
    results = list(results)
    activity_columns = max(1, max((len(item.activity_log_urls) for item in results), default=0))
    wiki_columns = max(1, max((len(item.wiki_urls) for item in results), default=0))
    providers = sorted(
        {provider for item in results for provider in item.provider_urls},
        key=lambda provider: (provider_label(provider).casefold(), provider),
    )
    has_warnings = any(item.warnings for item in results)
    return activity_columns, wiki_columns, providers, has_warnings


def numbered_header(base: str, index: int, total: int) -> str:
    return base if total == 1 else f"{base} {index}"


def write_csv(
    path: Path,
    nodes: list[Node],
    links_by_guid: dict[str, NodeLinks],
    web_base: str,
) -> None:
    activity_count, wiki_count, providers, has_warnings = dynamic_headers(
        links_by_guid.values()
    )
    headers = [
        "GUID",
        "Title",
        "URL",
        "Visibility",
        "Depth",
        "Parent GUID",
        "User permission",
        "Metadata download link",
        *[
            numbered_header("Activity Logs download link", index, activity_count)
            for index in range(1, activity_count + 1)
        ],
        *[
            numbered_header("Wiki download link", index, wiki_count)
            for index in range(1, wiki_count + 1)
        ],
    ]
    if providers:
        headers.extend(
            f"Files download as zip link — {provider_label(provider)}"
            for provider in providers
        )
    else:
        headers.append("Files download as zip link")
    if has_warnings:
        headers.append("Inspection warning")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for node in nodes:
            inspected = links_by_guid.get(node.guid, NodeLinks())
            row: dict[str, Any] = {
                "GUID": node.guid,
                "Title": node.title,
                "URL": node.url,
                "Visibility": "Public" if node.public else "Private",
                "Depth": node.depth,
                "Parent GUID": node.parent_guid or "",
                "User permission": node.permission,
                "Metadata download link": f"{web_base}/metadata/{node.guid}/",
            }
            for index in range(1, activity_count + 1):
                row[numbered_header("Activity Logs download link", index, activity_count)] = (
                    inspected.activity_log_urls[index - 1]
                    if index <= len(inspected.activity_log_urls)
                    else ""
                )
            for index in range(1, wiki_count + 1):
                row[numbered_header("Wiki download link", index, wiki_count)] = (
                    inspected.wiki_urls[index - 1]
                    if index <= len(inspected.wiki_urls)
                    else ""
                )
            if providers:
                for provider in providers:
                    row[f"Files download as zip link — {provider_label(provider)}"] = (
                        inspected.provider_urls.get(provider, "")
                    )
            else:
                row["Files download as zip link"] = ""
            if has_warnings:
                row["Inspection warning"] = " | ".join(inspected.warnings)
            writer.writerow(row)


def default_output() -> Path:
    downloads = Path.home() / "Downloads"
    parent = downloads if downloads.is_dir() else Path.cwd()
    return parent / "OSF Project Download Links.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a CSV of every accessible OSF project/component and its "
            "metadata, activity-log, wiki, and storage-provider download links."
        )
    )
    parser.add_argument("--output", type=Path, default=default_output())
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--limit",
        type=int,
        help="Inspect only the first N inventory records (useful for testing)",
    )
    parser.add_argument("--test", action="store_true", help="Use the OSF test environment")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}",
    )
    return parser.parse_args(argv)


def token_from_user(explicit: str | None) -> str:
    token = (explicit or os.environ.get("OSF_TOKEN") or "").strip()
    if token:
        return token
    token = getpass.getpass("OSF Personal Access Token: ").strip()
    if not token:
        raise ExportError("No OSF Personal Access Token was provided.")
    return token


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.workers > 16:
        raise ExportError("--workers must be between 1 and 16.")
    if args.limit is not None and args.limit < 1:
        raise ExportError("--limit must be at least 1.")

    api_base = TEST_API if args.test else PRODUCTION_API
    web_base = TEST_WEB if args.test else PRODUCTION_WEB
    client = OSFClient(api_base, token_from_user(args.token))

    print("Checking OSF account...", flush=True)
    print(f"Authenticated as: {account_name(client)}", flush=True)
    print("Retrieving project/component inventory...", flush=True)
    nodes = load_inventory(client, args.limit)
    if not nodes:
        raise ExportError("OSF returned no accessible projects or components.")

    print(f"Reviewing links for {len(nodes):,} projects/components...", flush=True)
    links_by_guid = inspect_all(client, nodes, args.workers)
    output = args.output.expanduser().resolve()
    write_csv(output, nodes, links_by_guid, web_base)

    warning_count = sum(bool(item.warnings) for item in links_by_guid.values())
    print(f"Created: {output}", flush=True)
    print(f"Rows: {len(nodes):,}", flush=True)
    if warning_count:
        print(
            f"Inspection warnings: {warning_count:,} row(s); see the final CSV column.",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except ExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
