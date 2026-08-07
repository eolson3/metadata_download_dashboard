import errno
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import osf_export_checklist_metadata_all_projects_v0_15 as exporter


class MetadataClient:
    api_base = "https://api.osf.io/v2"

    def api_url(self, path):
        return f"{self.api_base}/{path.lstrip('/')}"

    def get_json(self, url):
        guid = url.rstrip("/").split("/")[-1]
        return {
            "data": {
                "id": guid,
                "type": "nodes",
                "attributes": {
                    "title": "Project",
                    "public": False,
                    "category": "project",
                    "description": "Description",
                    "current_user_permissions": ["read", "write", "admin"],
                },
                "relationships": {},
                "links": {"html": f"https://osf.io/{guid}/"},
            }
        }

    def get_optional_json(self, url, absent_statuses=None):
        return None


class FileZipHandler(BaseHTTPRequestHandler):
    base = ""

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v2/nodes/abc12/":
            document = {
                "data": {
                    "id": "abc12",
                    "type": "nodes",
                    "attributes": {
                        "title": "Project",
                        "public": False,
                        "category": "project",
                        "current_user_permissions": ["read", "write", "admin"],
                    },
                    "relationships": {
                        "children": {
                            "links": {
                                "related": {
                                    "href": f"{self.base}/v2/nodes/abc12/children/"
                                }
                            }
                        },
                        "files": {
                            "links": {
                                "related": {
                                    "href": f"{self.base}/v2/nodes/abc12/files/"
                                }
                            }
                        },
                    },
                    "links": {"html": "https://osf.io/abc12/"},
                }
            }
            body = json.dumps(document).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v2/nodes/abc12/children/":
            body = json.dumps({"data": [], "links": {"next": None}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v2/nodes/abc12/files/":
            document = {
                "data": [
                    {
                        "id": "osfstorage",
                        "type": "files",
                        "attributes": {"provider": "osfstorage", "name": "osfstorage"},
                        "links": {"upload": f"{self.base}/files/abc12/osfstorage/"},
                        "relationships": {
                            "files": {
                                "links": {
                                    "related": {
                                        "href": f"{self.base}/v2/files/abc12/osfstorage/"
                                    }
                                }
                            }
                        },
                    }
                ],
                "links": {"next": None},
            }
            body = json.dumps(document).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/files/abc12/osfstorage/":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("example.txt", "OSF file content")
            body = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class DashboardV015Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FileZipHandler)
        FileZipHandler.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def node(self, guid="abc12", title="Project", permission="admin"):
        return exporter.NodeRecord(
            guid=guid,
            title=title,
            raw={
                "attributes": {
                    "current_user_permissions": ["read", "write", permission]
                },
                "relationships": {
                    "files": {
                        "links": {
                            "related": {
                                "href": f"{FileZipHandler.base}/v2/nodes/{guid}/files/"
                            }
                        }
                    }
                },
            },
            public=False,
            category="project",
            url=f"https://osf.io/{guid}/",
            permission=permission,
        )

    def test_effective_permission_is_singular(self):
        self.assertEqual(
            exporter.effective_permission(["read", "write", "admin"]), "admin"
        )
        self.assertEqual(exporter.effective_permission(["read", "write"]), "write")
        self.assertEqual(exporter.effective_permission(["read"]), "read")

    def test_batch_request_accepts_files_with_other_actions_and_projects(self):
        guids, actions = exporter.normalize_job_request(
            {
                "guids": ["ABC12", "def34"],
                "actions": ["metadata", "files"],
            },
            {"abc12", "def34"},
        )
        self.assertEqual(guids, ["abc12", "def34"])
        self.assertEqual(actions, ["metadata", "files"])

    def test_top_level_review_check_covers_the_component_tree(self):
        root = self.node()
        component = self.node("def34", "Component", "write")
        component.parent_guid = root.guid
        root.children = [component]
        with tempfile.TemporaryDirectory() as folder:
            app = exporter.ChecklistApp(
                MetadataClient(),
                [root],
                "OSF User",
                "user1",
                Path(folder),
            )
            app.set_tree_check(root.guid, True)
            self.assertEqual(set(app.checks), {root.guid, component.guid})
            app.set_tree_check(root.guid, False)
            self.assertEqual(app.checks, {})
            app.work_queue.put(None)

    def test_metadata_is_one_comprehensive_export_without_api_response_files(self):
        root = self.node()
        client = MetadataClient()
        job = exporter.ExportJob("job-metadata", root.guid, root.title, "metadata")
        issues = []
        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "metadata")
            count, _ = exporter.export_metadata(
                client,
                root,
                [root],
                job,
                issues,
                lambda progress, message: None,
                0.0,
                1.0,
                all_nodes=[root],
            )
            metadata = root_folder / "Metadata"
            self.assertEqual(count, 1)
            self.assertEqual(issues, [])
            self.assertTrue(any(metadata.glob("*Complete Metadata.json")))
            self.assertTrue(any(metadata.glob("*Complete Metadata.html")))
            self.assertFalse((metadata / "API Responses").exists())
            self.assertFalse((root_folder / "Metadata Summary").exists())

    def test_file_action_streams_a_valid_provider_zip(self):
        root = self.node()
        client = exporter.OSFClient(f"{FileZipHandler.base}/v2", "test-token")
        job = exporter.ExportJob("job1", root.guid, root.title, "files")
        issues = []
        with tempfile.TemporaryDirectory() as folder:
            root_folder = Path(folder) / root.display_name
            exporter.ensure_hierarchy(root, root_folder, "files")
            count, byte_count = exporter.export_file_archives(
                client,
                [root],
                job,
                issues,
                lambda progress, message: None,
                0.0,
                1.0,
            )
            archives = list((root_folder / "Files").glob("*.zip"))
            self.assertEqual(count, 1)
            self.assertGreater(byte_count, 0)
            self.assertEqual(issues, [])
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertEqual(archive.read("example.txt"), b"OSF file content")

    def test_file_job_finishes_and_writes_summary(self):
        root = self.node()
        client = exporter.OSFClient(f"{FileZipHandler.base}/v2", "test-token")
        with tempfile.TemporaryDirectory() as folder:
            app = exporter.ChecklistApp(
                client,
                [root],
                "OSF User",
                "user1",
                Path(folder),
            )
            job = app.queue_job("abc12", "files")
            deadline = time.time() + 10
            while time.time() < deadline and job.status in {"queued", "running"}:
                time.sleep(0.05)
            app.work_queue.put(None)
            self.assertEqual(job.status, "completed", job.issues)
            self.assertEqual(job.message, "Completed — all content succeeded.")
            self.assertEqual(set(app.public_state()), {"jobs"})
            summary = next(Path(folder).rglob("*Files as ZIP Export Summary.json"))
            document = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(document["counts"]["file_zip_archives"], 1)
            self.assertGreater(document["counts"]["file_zip_bytes"], 0)

    def test_dashboard_filters_permissions_and_selects_only_visible_roots(self):
        admin = self.node()
        component = self.node("ghi56", "Component", "write")
        component.parent_guid = admin.guid
        admin.children = [component]
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
        self.assertEqual(page.count('<input class="node-check"'), 2)
        self.assertIn('class="review-check-space"', page)
        self.assertIn("top-level projects reviewed", page)
        self.assertIn("Top-level projects", page)
        self.assertNotIn("Top-level export trees", page)
        self.assertNotIn("selectable export edition", page)
        self.assertIn("Version 0.15.0", page)
        self.assertIn("Select all matching top-level projects", page)
        self.assertIn(
            'querySelectorAll(".node[data-root=true] > .node-row .batch-check")',
            page,
        )
        self.assertIn(
            '.node[data-root="true"][data-filter-match="true"]:not(.hidden) > '
            ".node-row .batch-check",
            page,
        )
        self.assertIn('data-action="files"', page)
        self.assertIn('name="batchAction" value="files"', page)
        self.assertIn('name="batchAction" value="metadata"', page)
        self.assertNotIn("Separate API-response files", page)
        self.assertNotIn('value="metadata_summary"', page)
        self.assertNotIn('value="metadata_archive"', page)
        self.assertNotIn('id="selectedDataSize"', page)
        self.assertNotIn('id="estimatedDownloadTime"', page)
        self.assertNotIn('id="downloadSpeed"', page)
        self.assertNotIn('data-storage-guid="abc12"', page)
        self.assertNotIn("storage_sizes", page)
        self.assertNotIn("storage_scan", page)
        self.assertIn("access_permission", page)
        if shutil.which("node"):
            script = page.split("<script>", 1)[1].split("</script>", 1)[0]
            checked = subprocess.run(
                ["node", "--check"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
    def test_safe_segment_handles_cross_platform_filename_rules(self):
        self.assertEqual(exporter.safe_segment("CON"), "_CON")
        self.assertEqual(exporter.safe_segment("CON.txt"), "_CON.txt")
        self.assertEqual(exporter.safe_segment("NUL.json"), "_NUL.json")
        self.assertEqual(exporter.safe_segment("LPT1.zip"), "_LPT1.zip")
        self.assertEqual(exporter.safe_segment(".."), "Untitled")
        self.assertEqual(exporter.safe_segment("a/b\\c"), "a - b - c")

        unicode_name = exporter.safe_segment("😀" * 200)
        self.assertLessEqual(len(unicode_name.encode("utf-8")), 140)
        self.assertFalse(unicode_name.endswith((" ", ".")))

    def test_owner_prefixed_name_respects_byte_limit(self):
        node = self.node()
        node.title = "😀" * 200

        filename = exporter.owner_prefixed_name(
            node,
            " - Complete Metadata.json",
        )

        self.assertLessEqual(len(filename.encode("utf-8")), 220)
        self.assertIn("[abc12]", filename)
        self.assertTrue(filename.endswith(" - Complete Metadata.json"))

    def test_long_path_error_has_actionable_guidance(self):
        error = OSError(errno.ENAMETOOLONG, "File name too long")
        message = exporter.concise_issue_reason(error)

        self.assertIn("path exceeded", message)
        self.assertIn("--output", message)
    
    def test_certificate_error_has_specific_guidance(self):
        ssl_error = urllib.error.URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )
        message = str(exporter.connection_error(ssl_error))
        self.assertIn("Install Certificates.command", message)


if __name__ == "__main__":
    unittest.main()
