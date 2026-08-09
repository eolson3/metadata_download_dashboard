#!/usr/bin/env python3
"""Regression tests for OSF Export Checklist 0.15.0.

The suite uses only Python's standard library and makes no live OSF requests.
Place it beside the exporter and run:

    python3 test_osf_export_checklist_v0_15.py -v

To select an exact exporter file when several copies exist:

    OSF_EXPORTER_PATH=/path/to/exporter.py \
        python3 test_osf_export_checklist_v0_15.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import zipfile
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


CANONICAL_SOURCE_NAME = "osf_export_checklist_metadata_all_projects_v0_15.py"
DOWNLOADED_SOURCE_NAME = "osf_export_checklist_metadata_all_projects_v0_15(1).py"
_LOADED_MODULE: ModuleType | None = None
_LOADED_PATH: Path | None = None


def exporter_path() -> Path:
    """Resolve one unambiguous exporter source file."""
    explicit = os.environ.get("OSF_EXPORTER_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"OSF_EXPORTER_PATH is not a file: {path}")
        return path

    test_folder = Path(__file__).resolve().parent
    candidates = [
        test_folder / CANONICAL_SOURCE_NAME,
        test_folder / DOWNLOADED_SOURCE_NAME,
    ]
    matches = [path.resolve() for path in candidates if path.is_file()]
    matches = list(dict.fromkeys(matches))

    if not matches:
        raise FileNotFoundError(
            "Place the exporter beside this test script or set OSF_EXPORTER_PATH."
        )
    if len(matches) > 1:
        listed = ", ".join(path.name for path in matches)
        raise RuntimeError(
            f"More than one exporter candidate was found ({listed}). "
            "Set OSF_EXPORTER_PATH to the exact file to test."
        )
    return matches[0]


def compile_exporter(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def load_exporter() -> ModuleType:
    """Compile and import the exporter without relying on its filename."""
    global _LOADED_MODULE, _LOADED_PATH

    path = exporter_path()
    if _LOADED_MODULE is not None and _LOADED_PATH == path:
        return _LOADED_MODULE

    compile_exporter(path)
    import importlib.util

    module_name = "_osf_exporter_under_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create an import specification for {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    _LOADED_MODULE = module
    _LOADED_PATH = path
    return module


def api_relationship(guid: str | None) -> dict[str, object]:
    if not guid:
        return {"data": None}
    return {"data": {"id": guid, "type": "nodes"}}


def api_node(
    guid: str,
    title: str,
    parent_guid: str | None = None,
    *,
    permission: str = "admin",
    public: bool = False,
) -> dict[str, object]:
    relationships: dict[str, object] = {}
    if parent_guid is not None:
        relationships["parent"] = api_relationship(parent_guid)
    return {
        "id": guid,
        "type": "nodes",
        "attributes": {
            "title": title,
            "public": public,
            "category": "project" if parent_guid is None else "component",
            "date_modified": "2026-08-01T12:00:00.000000Z",
            "current_user_permissions": ["read", "write", permission],
        },
        "relationships": relationships,
        "links": {"html": f"https://osf.io/{guid}/"},
    }


def make_http_error(
    code: int,
    body: dict[str, object] | bytes | str,
    *,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    if isinstance(body, dict):
        body_bytes = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body
    return urllib.error.HTTPError(
        "https://api.osf.io/v2/example/",
        code,
        "test error",
        headers,
        io.BytesIO(body_bytes),
    )


class SourceValidationTests(unittest.TestCase):
    def test_exporter_source_compiles(self) -> None:
        try:
            path = exporter_path()
            compile_exporter(path)
        except (FileNotFoundError, RuntimeError, SyntaxError) as error:
            self.fail(str(error))


class ExporterTestCase(unittest.TestCase):
    exporter: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.exporter = load_exporter()
        except (FileNotFoundError, RuntimeError, SyntaxError) as error:
            raise unittest.SkipTest(
                f"Functional tests require a compilable exporter: {error}"
            ) from error

    def node(
        self,
        guid: str = "abc12",
        title: str = "Project",
        permission: str = "admin",
    ):
        exporter = self.exporter
        return exporter.NodeRecord(
            guid=guid,
            title=title,
            raw=api_node(guid, title, permission=permission),
            public=False,
            category="project",
            date_modified="2026-08-01T12:00:00.000000Z",
            url=f"https://osf.io/{guid}/",
            permission=permission,
        )


class ConfigurationAndFilenameTests(ExporterTestCase):
    def test_expected_export_actions_and_delays_are_configured(self) -> None:
        exporter = self.exporter
        self.assertEqual(exporter.SCRIPT_VERSION, "0.15.0")
        self.assertEqual(
            exporter.VALID_ACTIONS,
            {"metadata", "wiki_current", "wiki_history", "logs", "files"},
        )
        self.assertEqual(exporter.REQUEST_PAUSE_SECONDS, 0.5)
        self.assertGreaterEqual(exporter.FILE_ARCHIVE_PAUSE_SECONDS, 1.0)
        self.assertEqual(exporter.JSONAPI_MEDIA_TYPE, "application/vnd.api+json")

    def test_safe_segment_is_portable_across_common_filesystems(self) -> None:
        exporter = self.exporter
        self.assertEqual(exporter.safe_segment("CON.txt"), "_CON.txt")
        self.assertEqual(exporter.safe_segment("  ...  "), "Untitled")
        self.assertEqual(exporter.safe_segment("e\u0301"), "é")

        cleaned = exporter.safe_segment('a<b>:c"d/e\\f|g?h*i\x80j')
        self.assertFalse(exporter.INVALID_FILENAME_CHARS.search(cleaned))
        self.assertFalse(cleaned.endswith((" ", ".")))

    def test_filename_limits_are_utf8_byte_limits_and_retain_guid(self) -> None:
        exporter = self.exporter
        segment = exporter.safe_segment("😀" * 20, max_length=17)
        self.assertLessEqual(len(segment.encode("utf-8")), 17)
        segment.encode("utf-8").decode("utf-8")

        name = exporter.title_guid_name("😀" * 40, "abc12", max_length=40)
        self.assertTrue(name.endswith(" [abc12]"))
        self.assertLessEqual(len(name.encode("utf-8")), 40)

    def test_long_path_error_recommends_a_shorter_output_location(self) -> None:
        exporter = self.exporter
        error = OSError(36, "File name too long")
        self.assertIn("shorter --output", exporter.concise_issue_reason(error))


class HTTPClientTests(ExporterTestCase):
    def test_authorization_is_sent_only_to_trusted_hosts(self) -> None:
        exporter = self.exporter
        client = exporter.OSFClient("https://api.osf.io/v2", " secret-token ")

        trusted = client.headers("https://api.osf.io/v2/users/me/")
        untrusted = client.headers("https://example.org/download")
        self.assertEqual(trusted["Authorization"], "Bearer secret-token")
        self.assertNotIn("Authorization", untrusted)
        self.assertEqual(trusted["Accept"], exporter.JSONAPI_MEDIA_TYPE)

    def test_client_pauses_between_requests(self) -> None:
        exporter = self.exporter
        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        response = mock.Mock()
        client.opener.open = mock.Mock(return_value=response)

        with mock.patch.object(exporter.time, "sleep") as sleep:
            self.assertIs(client.open(client.api_url("users/me/")), response)
            sleep.assert_not_called()
            self.assertIs(client.open(client.api_url("users/me/nodes/")), response)
            sleep.assert_called_once_with(exporter.REQUEST_PAUSE_SECONDS)

    def test_default_sort_and_page_size_are_added_without_overrides(self) -> None:
        exporter = self.exporter
        url = exporter.add_page_size(
            "https://api.osf.io/v2/nodes/?filter[public]=true",
            default_sort="-date_modified",
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["page[size]"], [str(exporter.PAGE_SIZE)])
        self.assertEqual(query["sort"], ["-date_modified"])

        explicit = exporter.add_page_size(
            "https://api.osf.io/v2/nodes/?page[size]=25&sort=title",
            default_sort="-date_modified",
        )
        explicit_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(explicit).query
        )
        self.assertEqual(explicit_query["page[size]"], ["25"])
        self.assertEqual(explicit_query["sort"], ["title"])

    def test_paginate_applies_defaults_to_every_page(self) -> None:
        exporter = self.exporter
        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        calls: list[str] = []

        def get_json(url: str) -> dict[str, object]:
            calls.append(url)
            if len(calls) == 1:
                return {
                    "data": [{"id": "one"}],
                    "links": {"next": "https://api.osf.io/v2/nodes/?page=2"},
                }
            return {"data": [{"id": "two"}], "links": {"next": None}}

        client.get_json = get_json  # type: ignore[method-assign]
        records = list(
            client.paginate(
                "https://api.osf.io/v2/nodes/",
                default_sort="title",
            )
        )
        self.assertEqual([item["id"] for item in records], ["one", "two"])
        self.assertEqual(len(calls), 2)
        for url in calls:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(query["page[size]"], [str(exporter.PAGE_SIZE)])
            self.assertEqual(query["sort"], ["title"])

    def test_paginate_rejects_a_repeated_next_link(self) -> None:
        exporter = self.exporter
        client = exporter.OSFClient("https://api.osf.io/v2", "token")

        def get_json(url: str) -> dict[str, object]:
            return {"data": [], "links": {"next": url}}

        client.get_json = get_json  # type: ignore[method-assign]
        with self.assertRaisesRegex(exporter.ChecklistError, "repeated"):
            list(client.paginate("https://api.osf.io/v2/nodes/"))

    def test_retry_after_is_not_artificially_capped(self) -> None:
        exporter = self.exporter
        error = make_http_error(429, {}, retry_after="900")
        self.assertEqual(exporter.retry_delay(error, 0), 900.0)

    def test_maintenance_mode_uses_minimum_delay_and_status_guidance(self) -> None:
        exporter = self.exporter
        error = make_http_error(
            503,
            {"meta": {"maintenance_mode": True}},
            retry_after="30",
        )
        self.assertTrue(exporter.osf_maintenance_mode(error))
        self.assertEqual(
            exporter.retry_delay(error, 0),
            float(exporter.MIN_MAINTENANCE_RETRY_SECONDS),
        )
        message = str(exporter.http_error(error))
        self.assertIn("undergoing maintenance", message)
        self.assertIn(exporter.OSF_STATUS_URL, message)

        longer = make_http_error(
            503,
            {"meta": {"maintenance_mode": True}},
            retry_after="900",
        )
        self.assertEqual(exporter.retry_delay(longer, 0), 900.0)

    def test_certificate_error_has_specific_guidance(self) -> None:
        exporter = self.exporter
        ssl_error = urllib.error.URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )
        self.assertIn(
            "Install Certificates.command",
            str(exporter.connection_error(ssl_error)),
        )

    def test_download_file_streams_and_validates_a_zip(self) -> None:
        exporter = self.exporter
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("example.txt", "OSF content")

        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        client.opener.open = mock.Mock(return_value=io.BytesIO(buffer.getvalue()))
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "provider.zip"
            size = client.download_file(
                "https://files.osf.io/provider/?zip=",
                destination,
            )
            self.assertEqual(size, destination.stat().st_size)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.read("example.txt"), b"OSF content")

    def test_download_file_does_not_replace_an_existing_zip_with_an_empty_zip(
        self,
    ) -> None:
        exporter = self.exporter
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED):
            pass

        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        client.opener.open = mock.Mock(return_value=io.BytesIO(buffer.getvalue()))
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "provider.zip"
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("previous.txt", "previous content")
            previous = destination.read_bytes()
            size = client.download_file(
                "https://files.osf.io/provider/?zip=",
                destination,
            )
            self.assertEqual(size, 0)
            self.assertEqual(destination.read_bytes(), previous)

    def test_download_file_does_not_replace_an_existing_zip_with_directories_only(
        self,
    ) -> None:
        exporter = self.exporter
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("empty-folder/", b"")

        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        client.opener.open = mock.Mock(return_value=io.BytesIO(buffer.getvalue()))
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "provider.zip"
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("previous.txt", "previous content")
            previous = destination.read_bytes()
            size = client.download_file(
                "https://files.osf.io/provider/?zip=",
                destination,
            )
            self.assertEqual(size, 0)
            self.assertEqual(destination.read_bytes(), previous)

    def test_download_file_does_not_retry_an_invalid_zip_response(self) -> None:
        exporter = self.exporter
        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        client.opener.open = mock.Mock(return_value=io.BytesIO(b"incomplete ZIP"))
        client.wait = mock.Mock()

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "provider.zip"
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("previous.txt", "previous content")
            previous = destination.read_bytes()
            with self.assertRaises(exporter.ChecklistError):
                client.download_file(
                    "https://files.osf.io/provider/?zip=",
                    destination,
                )

            self.assertEqual(client.opener.open.call_count, 1)
            client.wait.assert_not_called()
            self.assertEqual(destination.read_bytes(), previous)

    def test_download_file_rejects_a_shallow_valid_zip_if_inspection_fails(
        self,
    ) -> None:
        exporter = self.exporter
        downloaded = b"PK shallow-valid archive"
        client = exporter.OSFClient("https://api.osf.io/v2", "token")
        client.opener.open = mock.Mock(return_value=io.BytesIO(downloaded))

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "provider.zip"
            destination.write_bytes(b"invalid result from an earlier run")
            with (
                mock.patch.object(exporter.zipfile, "is_zipfile", return_value=True),
                mock.patch.object(
                    exporter.zipfile,
                    "ZipFile",
                    side_effect=zipfile.BadZipFile("central directory"),
                ),
            ):
                with self.assertRaises(exporter.ChecklistError):
                    client.download_file(
                        "https://files.osf.io/provider/?zip=",
                        destination,
                    )

            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_osf_storage_uses_paginated_download_as_zip(self) -> None:
        exporter = self.exporter
        provider, _, _, download_url = exporter.provider_archive_details(
            {
                "id": "osfstorage",
                "attributes": {"provider": "osfstorage"},
                "links": {"upload": "https://files.osf.io/osfstorage/"},
            }
        )
        self.assertEqual(provider, "osfstorage")
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(download_url).query,
            keep_blank_values=True,
        )
        self.assertEqual(query["zip"], [""])
        self.assertEqual(query["paginated"], ["true"])
        self.assertEqual(
            query["limit"],
            [str(exporter.OSF_STORAGE_DAZ_PAGE_SIZE)],
        )

    def test_other_storage_providers_keep_the_standard_zip_url(self) -> None:
        exporter = self.exporter
        _, _, _, download_url = exporter.provider_archive_details(
            {
                "id": "s3",
                "attributes": {"provider": "s3"},
                "links": {"upload": "https://files.osf.io/s3/"},
            }
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(download_url).query,
            keep_blank_values=True,
        )
        self.assertEqual(query, {"zip": [""]})


class HierarchyTests(ExporterTestCase):
    def test_get_inventory_requests_a_stable_default_sort(self) -> None:
        exporter = self.exporter

        class InventoryClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.sort: str | None = None

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str, *, default_sort=None):
                self.sort = default_sort
                return iter([api_node("abc12", "Project")])

        client = InventoryClient()
        inventory = exporter.get_inventory(client)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(client.sort, "-date_modified")

    def test_discover_tree_uses_root_filter_and_rebuilds_hierarchy(self) -> None:
        exporter = self.exporter
        records = [
            api_node("ghi56", "Grandchild", "def34"),
            api_node("abc12", "Root"),
            api_node("def34", "Child", "abc12"),
        ]

        class TreeClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.urls: list[str] = []

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                self.urls.append(url)
                return iter(records)

            def get_json(self, url: str):
                raise AssertionError("Root fallback should not be needed")

        client = TreeClient()
        root = exporter.discover_tree(client, "ABC12")
        self.assertEqual([node.guid for node in exporter.flatten_tree(root)], [
            "abc12",
            "def34",
            "ghi56",
        ])
        self.assertEqual(len(client.urls), 1)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(client.urls[0]).query
        )
        self.assertEqual(query["filter[root]"], ["abc12"])

    def test_discover_tree_fetches_root_if_filter_omits_it(self) -> None:
        exporter = self.exporter
        child = api_node("def34", "Child", "abc12")

        class TreeClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.fallback_calls = 0

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                return iter([child])

            def get_json(self, url: str):
                self.fallback_calls += 1
                return {"data": api_node("abc12", "Root")}

        client = TreeClient()
        root = exporter.discover_tree(client, "abc12")
        self.assertEqual(client.fallback_calls, 1)
        self.assertEqual([child.guid for child in root.children], ["def34"])

    def test_discover_tree_rejects_duplicate_and_disconnected_nodes(self) -> None:
        exporter = self.exporter

        class TreeClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self, records):
                self.records = records

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                return iter(self.records)

            def get_json(self, url: str):
                return {"data": api_node("abc12", "Root")}

        duplicate = TreeClient([
            api_node("abc12", "Root"),
            api_node("abc12", "Root again"),
        ])
        with self.assertRaisesRegex(exporter.ChecklistError, "repeated node GUID"):
            exporter.discover_tree(duplicate, "abc12")

        disconnected = TreeClient([
            api_node("abc12", "Root"),
            api_node("def34", "Orphan", "zzzzz"),
        ])
        with self.assertRaisesRegex(exporter.ChecklistError, "could not be connected"):
            exporter.discover_tree(disconnected, "abc12")


class MetadataAndWikiTests(ExporterTestCase):
    class MetadataClient:
        api_base = "https://api.osf.io/v2"

        def api_url(self, path: str) -> str:
            return f"{self.api_base}/{path.lstrip('/')}"

        def get_json(self, url: str):
            guid = url.rstrip("/").split("/")[-1]
            return {"data": api_node(guid, "Project")}

        def get_optional_json(self, url: str, absent_statuses=None):
            return None

    def test_metadata_export_is_comprehensive_without_api_response_folder(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("metadata-job", root.guid, root.title, "metadata")
        issues: list[object] = []

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "metadata")
            count, related = exporter.export_metadata(
                self.MetadataClient(),
                root,
                [root],
                job,
                issues,
                lambda progress, message: None,
                0.0,
                1.0,
                all_nodes=[root],
            )
            metadata_folder = root_folder / "Metadata"
            self.assertEqual(count, 1)
            self.assertGreaterEqual(related, 0)
            self.assertEqual(issues, [])
            self.assertEqual(len(list(metadata_folder.glob("*Complete Metadata.json"))), 1)
            self.assertEqual(len(list(metadata_folder.glob("*Complete Metadata.html"))), 1)
            self.assertFalse((metadata_folder / "API Responses").exists())
            self.assertFalse((root_folder / "Metadata Summary").exists())

    def test_complete_metadata_html_contains_css_not_a_template_marker(self) -> None:
        exporter = self.exporter
        package = {
            "project": {"guid": "abc12", "title": "Project"},
            "core_api_response": {"data": api_node("abc12", "Project")},
            "exported_utc": "2026-08-07T12:00:00+00:00",
        }
        document = exporter.render_complete_metadata_html(package)
        self.assertNotIn("__DASHBOARD_CSS__", document)
        self.assertIn("<style>", document)
        self.assertIn("--ink:", document)

    def test_wiki_export_writes_current_pages_versions_and_indexes(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("wiki-job", root.guid, root.title, "wiki_history")
        issues: list[object] = []
        progress_messages: list[str] = []

        wikis = [
            {
                "id": "zebra",
                "attributes": {"name": "Zebra"},
                "links": {"download": "https://files.osf.io/wiki/zebra/current"},
                "relationships": {
                    "versions": {
                        "links": {
                            "related": {"href": "https://api.osf.io/v2/wikis/zebra/versions/"}
                        }
                    }
                },
            },
            {
                "id": "home1",
                "attributes": {"name": "Home"},
                "links": {"download": "https://files.osf.io/wiki/home/current"},
                "relationships": {
                    "versions": {
                        "links": {
                            "related": {"href": "https://api.osf.io/v2/wikis/home1/versions/"}
                        }
                    }
                },
            },
        ]

        def version(wiki_id: str, version_id: str, day: int):
            return {
                "id": version_id,
                "attributes": {
                    "date_created": f"2026-08-{day:02d}T12:00:00.000000Z",
                    "size": 12,
                    "content_type": "text/markdown",
                },
                "links": {
                    "download": f"https://files.osf.io/wiki/{wiki_id}/{version_id}",
                    "self": f"https://api.osf.io/v2/wikis/{wiki_id}/versions/{version_id}/",
                },
                "relationships": {"user": api_relationship("user1")},
            }

        class WikiClient:
            api_base = "https://api.osf.io/v2"

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                if "/nodes/" in url and "/wikis/" in url:
                    return iter(wikis)
                if "/home1/versions/" in url:
                    return iter([version("home1", "2", 2), version("home1", "1", 1)])
                if "/zebra/versions/" in url:
                    return iter([version("zebra", "1", 3)])
                raise AssertionError(f"Unexpected pagination URL: {url}")

            def get_text(self, url: str) -> str:
                return f"Downloaded from {url}\n"

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "wiki_current")
            exporter.ensure_hierarchy(root, root_folder, "wiki_history")
            pages, versions = exporter.export_wikis(
                WikiClient(),
                [root],
                job,
                issues,
                lambda progress, message: progress_messages.append(message),
                0.0,
                1.0,
                include_current=True,
                include_history=True,
            )
            self.assertEqual((pages, versions), (2, 3))
            self.assertEqual(issues, [])
            self.assertEqual(len(list((root_folder / "Wikis" / "Current").glob("*.md"))), 2)
            history = root_folder / "Wikis" / "Version History"
            self.assertEqual(len(list(history.rglob("*Version *.md"))), 3)
            self.assertEqual(len(list(history.rglob("*Version History.csv"))), 2)
            self.assertEqual(len(list(history.rglob("*Version History.json"))), 2)
            self.assertEqual(progress_messages[-1], "Wiki export finished")

    def test_wiki_export_requires_at_least_one_content_choice(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("wiki-job", root.guid, root.title, "wiki_current")
        with self.assertRaisesRegex(exporter.ChecklistError, "Choose current wikis"):
            exporter.export_wikis(
                mock.Mock(),
                [root],
                job,
                [],
                lambda progress, message: None,
                0.0,
                1.0,
                include_current=False,
                include_history=False,
            )


class LogsFilesAndRetryTests(ExporterTestCase):
    def log(
        self,
        log_id: str,
        *,
        original_guid: str | None = None,
        context_guid: str | None = None,
        title: str = "",
    ) -> dict[str, object]:
        relationships: dict[str, object] = {}
        if original_guid:
            relationships["original_node"] = api_relationship(original_guid)
        if context_guid:
            relationships["node"] = api_relationship(context_guid)
        params = {}
        if original_guid:
            params["params_node"] = {"id": original_guid, "title": title}
        return {
            "id": log_id,
            "attributes": {
                "date": f"2026-08-0{log_id[-1]}T12:00:00Z",
                "action": "project_created",
                "params": params,
            },
            "relationships": relationships,
            "links": {"self": f"https://api.osf.io/v2/logs/{log_id}/"},
        }

    def test_partition_logs_handles_current_former_unassigned_and_retry_skip(self) -> None:
        exporter = self.exporter
        root = self.node()
        child = self.node("def34", "Child", "write")
        node_map = {root.guid: root}
        full_node_map = {root.guid: root, child.guid: child}
        logs = [
            self.log("log1", original_guid="abc12", context_guid="abc12"),
            self.log("log2", original_guid="def34", context_guid="abc12"),
            self.log("log3", original_guid="old99", context_guid="abc12", title="Former"),
            self.log("log4"),
        ]
        current, former, unassigned = exporter.partition_logs(
            root,
            logs,
            node_map,
            full_node_map,
        )
        self.assertEqual([log["id"] for log in current["abc12"]], ["log1"])
        self.assertEqual(list(former), [("abc12", "old99")])
        self.assertEqual([log["id"] for log in unassigned], ["log4"])
        retained_ids = {
            str(log["id"])
            for group in current.values()
            for log in group
        }
        retained_ids.update(
            str(log["id"])
            for group in former.values()
            for log in group
        )
        retained_ids.update(str(log["id"]) for log in unassigned)
        self.assertNotIn("log2", retained_ids)

    def test_organize_logs_writes_each_class_to_the_expected_folder(self) -> None:
        exporter = self.exporter
        root = self.node()
        child = self.node("def34", "Child", "write")
        child.parent_guid = root.guid
        root.children = [child]
        logs = [
            self.log("log1", original_guid="abc12", context_guid="abc12"),
            self.log("log2", original_guid="old99", context_guid="abc12", title="Former"),
            self.log("log3"),
        ]
        issues: list[object] = []

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "logs")
            count = exporter.organize_logs(
                root,
                [root],
                logs,
                issues,
                all_nodes=[root, child],
            )
            self.assertEqual(count, 3)
            self.assertEqual(issues, [])
            log_folder = root_folder / "Activity Logs"
            self.assertTrue(any(log_folder.glob("*Activity Log.csv")))
            self.assertTrue(any(log_folder.rglob("Former or inaccessible components/**/*Activity Log.json")))
            self.assertTrue(any(log_folder.glob("*Unassigned*Activity Log.json")))

    def test_file_archives_pause_between_providers_only(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("file-job", root.guid, root.title, "files")
        issues: list[object] = []

        class ArchiveClient:
            api_base = "https://api.osf.io/v2"

            def trust_download_host(self, url: str) -> None:
                return None

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def get_json(self, url: str):
                return {"data": [{"id": "file"}]}

            def paginate(self, url: str):
                return iter([
                    {
                        "id": "osfstorage",
                        "attributes": {"provider": "osfstorage"},
                        "links": {"upload": "https://files.osf.io/osfstorage/"},
                    },
                    {
                        "id": "s3",
                        "attributes": {"provider": "s3"},
                        "links": {"upload": "https://files.osf.io/s3/"},
                    },
                ])

            def download_file(self, url: str, destination: Path, cancelled=None):
                destination.write_bytes(url.encode("utf-8"))
                return destination.stat().st_size

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            with mock.patch.object(exporter.time, "sleep") as sleep:
                count, byte_count = exporter.export_file_archives(
                    ArchiveClient(),
                    [root],
                    job,
                    issues,
                    lambda progress, message: None,
                    0.0,
                    1.0,
                )
            self.assertEqual(count, 2)
            self.assertGreater(byte_count, 0)
            self.assertEqual(issues, [])
            sleep.assert_called_once_with(exporter.FILE_ARCHIVE_PAUSE_SECONDS)
            self.assertEqual(len(list((root_folder / "Files").glob("*.zip"))), 2)

    def test_empty_file_provider_is_not_a_failed_archive(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("file-job", root.guid, root.title, "files")
        issues: list[object] = []

        class EmptyArchiveClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.download_file = mock.Mock()

            def trust_download_host(self, url: str) -> None:
                return None

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def get_json(self, url: str):
                return {"data": []}

            def paginate(self, url: str):
                return iter([
                    {
                        "id": "osfstorage",
                        "attributes": {"provider": "osfstorage"},
                        "links": {"upload": "https://files.osf.io/osfstorage/"},
                    }
                ])

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            client = EmptyArchiveClient()
            count, byte_count = exporter.export_file_archives(
                client,
                [root],
                job,
                issues,
                lambda progress, message: None,
                0.0,
                1.0,
            )
            self.assertEqual((count, byte_count), (0, 0))
            self.assertEqual(root.empty_file_provider_count, 1)
            self.assertEqual(issues, [])
            client.download_file.assert_not_called()
            self.assertFalse(any((root_folder / "Files").glob("*.zip")))

    def test_invalid_provider_zip_falls_back_to_smaller_daz_archives(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("file-job", root.guid, root.title, "files")
        issues: list[object] = []

        class SplitArchiveClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.download_urls: list[str] = []

            def trust_download_host(self, url: str) -> None:
                return None

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                return iter([
                    {
                        "id": "osfstorage",
                        "attributes": {"provider": "osfstorage"},
                        "links": {"upload": "https://files.osf.io/osfstorage/"},
                    }
                ])

            def get_json(self, url: str):
                return {
                    "data": [
                        {
                            "id": "folder1",
                            "attributes": {"name": "Folder One", "kind": "folder"},
                            "links": {
                                "upload": "https://files.osf.io/osfstorage/folder1/"
                            },
                        },
                        {
                            "id": "file1",
                            "attributes": {"name": "root.txt", "kind": "file"},
                            "links": {
                                "download": "https://files.osf.io/osfstorage/file1"
                            },
                        },
                    ]
                }

            def download_file(self, url: str, destination: Path, cancelled=None):
                self.download_urls.append(url)
                if urllib.parse.urlsplit(url).path == "/osfstorage/":
                    raise exporter.ChecklistError(
                        "The OSF file service returned an incomplete or invalid ZIP archive."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"valid split archive")
                return destination.stat().st_size

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            client = SplitArchiveClient()
            with mock.patch.object(exporter.time, "sleep"):
                count, byte_count = exporter.export_file_archives(
                    client,
                    [root],
                    job,
                    issues,
                    lambda progress, message: None,
                    0.0,
                    1.0,
                )

            self.assertEqual(count, 2)
            self.assertGreater(byte_count, 0)
            self.assertEqual(issues, [])
            self.assertEqual(root.file_archive_count, 2)
            self.assertEqual(len(client.download_urls), 3)
            self.assertEqual(
                sum(
                    urllib.parse.urlsplit(url).path == "/osfstorage/"
                    for url in client.download_urls
                ),
                1,
            )
            for url in client.download_urls:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(url).query,
                    keep_blank_values=True,
                )
                self.assertIn("zip", query)
            split_folder = root_folder / "Files" / "OSF Storage - Split ZIPs"
            self.assertEqual(len(list(split_folder.glob("*.zip"))), 2)

    def test_prior_invalid_provider_scope_skips_the_root_zip_attempt(self) -> None:
        exporter = self.exporter
        root = self.node()
        job = exporter.ExportJob("file-job", root.guid, root.title, "files")
        issues: list[object] = []

        class PriorFailureClient:
            api_base = "https://api.osf.io/v2"

            def __init__(self) -> None:
                self.download_urls: list[str] = []

            def trust_download_host(self, url: str) -> None:
                return None

            def api_url(self, path: str) -> str:
                return f"{self.api_base}/{path.lstrip('/')}"

            def paginate(self, url: str):
                return iter([
                    {
                        "id": "osfstorage",
                        "attributes": {"provider": "osfstorage"},
                        "links": {"upload": "https://files.osf.io/osfstorage/"},
                    }
                ])

            def get_json(self, url: str):
                return {
                    "data": [
                        {
                            "id": "folder1",
                            "attributes": {"name": "Folder One", "kind": "folder"},
                            "links": {
                                "upload": "https://files.osf.io/osfstorage/folder1/"
                            },
                        }
                    ]
                }

            def download_file(self, url: str, destination: Path, cancelled=None):
                self.download_urls.append(url)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"valid split archive")
                return destination.stat().st_size

        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            invalid_root_archive = root_folder / "Files" / exporter.owner_prefixed_name(
                root,
                " - Files - OSF Storage.zip",
            )
            invalid_root_archive.write_bytes(b"invalid prior provider archive")
            client = PriorFailureClient()
            count, _ = exporter.export_file_archives(
                client,
                [root],
                job,
                issues,
                lambda progress, message: None,
                0.0,
                1.0,
                split_providers={(root.guid, "osfstorage")},
            )

            self.assertEqual(count, 1)
            self.assertEqual(issues, [])
            self.assertEqual(len(client.download_urls), 1)
            self.assertEqual(
                urllib.parse.urlsplit(client.download_urls[0]).path,
                "/osfstorage/folder1/",
            )
            self.assertFalse(invalid_root_archive.exists())

    def test_previous_invalid_zip_summary_enables_split_mode(self) -> None:
        exporter = self.exporter
        root = self.node()
        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            summary = root_folder / "Files" / (
                f"{root.display_name} - Files as ZIP Export Summary.json"
            )
            summary.write_text(
                json.dumps(
                    {
                        "issues": [
                            {
                                "project_guid": root.guid,
                                "element_id": "osfstorage",
                                "detail": (
                                    "The OSF file service returned an incomplete or "
                                    "invalid ZIP archive."
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                exporter.prior_invalid_file_archive_scopes(root),
                {(root.guid, "osfstorage")},
            )

    def test_targeted_retry_includes_only_failed_log_scope(self) -> None:
        exporter = self.exporter
        issue = exporter.ExportIssue(
            severity="critical",
            project_guid="def34",
            project_title="Child",
            element_type="activity-log collection",
            element_id="def34",
            reason="OSF returned HTTP 502",
            detail="HTTP 502",
        )
        plan = exporter.build_retry_plan("logs", [issue], "failed")
        self.assertEqual(plan["mode"], "targeted")
        self.assertEqual(plan["scopes"], {"logs": ["def34"]})

    def test_grouped_issue_names_the_failed_element(self) -> None:
        exporter = self.exporter
        issue = exporter.ExportIssue(
            severity="critical",
            project_guid="def34",
            project_title="Child",
            element_type="activity-log collection",
            element_id="def34",
            reason="OSF continued returning a bad gateway error after automatic retries (HTTP 502)",
            detail="HTTP 502",
        )
        grouped = exporter.group_export_issues([issue])
        self.assertIn("activity-log collection", grouped[0]["message"])
        self.assertIn("Child [def34]", grouped[0]["message"])
        self.assertEqual(grouped[0]["element_ids"], ["def34"])


class DashboardTests(ExporterTestCase):
    def test_review_check_on_a_root_covers_its_components(self) -> None:
        exporter = self.exporter
        root = self.node()
        child = self.node("def34", "Child", "write")
        child.parent_guid = root.guid
        root.children = [child]

        with tempfile.TemporaryDirectory() as folder:
            app = exporter.ChecklistApp(
                mock.Mock(),
                [root],
                "OSF User",
                "user1",
                Path(folder),
            )
            try:
                app.set_tree_check(root.guid, True)
                self.assertEqual(set(app.checks), {"abc12", "def34"})
                app.set_tree_check(root.guid, False)
                self.assertEqual(app.checks, {})
            finally:
                app.work_queue.put(None)
                app.worker.join(timeout=2)

    def test_dashboard_has_current_filters_batch_options_and_root_checks(self) -> None:
        exporter = self.exporter
        admin = self.node()
        child = self.node("ghi56", "Component", "write")
        child.parent_guid = admin.guid
        admin.children = [child]
        read = self.node("def34", "Read Project", "read")
        app = SimpleNamespace(
            roots=[admin, read],
            checks={},
            account_name="OSF User",
            session_key="session-key",
            output_base=Path("/tmp/OSF Project Exports"),
        )

        page = exporter.render_html(app)
        self.assertIn('id="permission"', page)
        self.assertIn('data-permission="admin"', page)
        self.assertIn('data-permission="read"', page)
        self.assertIn("Top-level projects", page)
        self.assertIn("Select all matching top-level projects", page)
        self.assertIn('[data-filter-match="true"]', page)
        self.assertIn('name="batchAction" value="metadata"', page)
        self.assertIn('name="batchAction" value="wiki_current"', page)
        self.assertIn('name="batchAction" value="wiki_history"', page)
        self.assertIn('name="batchAction" value="logs"', page)
        self.assertIn('name="batchAction" value="files"', page)
        self.assertEqual(page.count('<input class="node-check"'), 2)
        self.assertIn('class="review-check-space"', page)
        self.assertNotIn("selectable export edition", page)
        self.assertNotIn("Top-level export trees", page)
        self.assertNotIn("Separate API-response files", page)
        self.assertNotIn("__DASHBOARD_CSS__", page)
        self.assertIn("<style>", page)

    def test_batch_request_accepts_files_with_other_actions(self) -> None:
        exporter = self.exporter
        guids, actions = exporter.normalize_job_request(
            {
                "guids": ["ABC12", "def34"],
                "actions": ["metadata", "files", "wiki_current"],
            },
            {"abc12", "def34"},
        )
        self.assertEqual(guids, ["abc12", "def34"])
        self.assertEqual(actions, ["metadata", "files", "wiki_current"])


if __name__ == "__main__":
    unittest.main()
