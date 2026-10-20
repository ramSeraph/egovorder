import contextlib
import datetime
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from egovorder.archive import OrderIA
from egovorder import iasync
from egovorder.srcs.datasrcs_info import srcinfos
from egovorder.utils.file_storage import FileManager
from egovorder.utils.metainfo import MetaInfo


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = FileManager(self.temp.name, False, False)
        self.path = "andhra_goir/2025-06-01/1-12345"
        self.metadata = MetaInfo()
        self.metadata.update(
            date=datetime.date(2025, 6, 1), department="Finance", gotype="Manuscript",
            gonum="42", abstract="Allocation & expenditure", category="Service",
            section="Budget", amount="1000",
        )
        self.metadata.set_url("https://goir.ap.gov.in/dgo.ashx?gid=12345&fileType=1")
        self.storage.save_rawdoc("andhra_goir", self.path, None, b"%PDF-1.4\n%%EOF\n")
        self.storage.save_metainfo("andhra_goir", self.path, self.metadata)
        with patch("egovorder.archive.get_session"):
            self.archive = OrderIA(self.storage, "fake-access", "fake-secret", "info", None, False)

    def test_metadata_keeps_goir_collection_title_and_fields(self):
        for source, collection in (("andhra_goir", "andhra-goir"), ("telangana_goir", "telangana-goir")):
            with self.subTest(source=source):
                result = self.metadata.get_ia_metadata(srcinfos[source], False)
                self.assertEqual(result["collection"], collection)
                self.assertEqual(result["creator"], srcinfos[source]["source"])
                self.assertEqual(result["language"], ["eng", "tel"])
                self.assertEqual(result["date"], "2025-06-01")
                self.assertIn("Manuscript, Number 42", result["title"])
                self.assertIn("Order Number", result["description"])
                self.assertNotIn("Gazette", result["description"])
                self.assertEqual(
                    self.metadata.get_ia_metadata(srcinfos[source], True)["collection"],
                    "test_collection",
                )

    def test_new_upload_uses_original_identifier_and_local_files(self):
        with patch.object(self.archive, "get_ia_item", return_value=SimpleNamespace(exists=False)):
            with patch("egovorder.archive.upload") as upload:
                self.assertTrue(self.archive.upload(self.path))
        args, kwargs = upload.call_args
        self.assertEqual(args[0], "in.gov.andhra.goir.2025-06-01.1-12345")
        self.assertEqual(args[1], [self.storage.get_rawfile_path(self.path),
                                  self.storage.get_metafile_path(self.path)])
        self.assertEqual(kwargs["metadata"]["collection"], "andhra-goir")

    def test_already_archived_files_are_successful_without_reupload(self):
        files = [SimpleNamespace(name=Path(path).name) for path in (
            self.storage.get_rawfile_path(self.path), self.storage.get_metafile_path(self.path),
        )]
        item = SimpleNamespace(exists=True, get_files=lambda: files)
        with patch.object(self.archive, "get_ia_item", return_value=item):
            with patch("egovorder.archive.upload") as upload:
                self.assertTrue(self.archive.upload(self.path))
                upload.assert_not_called()

    def test_sandbox_upload_preserves_the_sandbox_identifier(self):
        self.archive.to_sandbox = True
        with patch.object(self.archive, "get_ia_item", return_value=SimpleNamespace(exists=False)):
            with patch("egovorder.archive.upload") as upload:
                self.assertTrue(self.archive.upload(self.path))
        self.assertEqual(upload.call_args.args[0], "egaztest.in.gov.andhra.goir.2025-06-01.1-12345")
        self.assertEqual(upload.call_args.kwargs["metadata"]["collection"], "test_collection")

    def test_update_metadata_and_xml_use_the_same_item(self):
        with patch.object(self.archive, "get_ia_item", return_value=SimpleNamespace(exists=True)):
            with patch("egovorder.archive.modify_metadata") as modify, patch("egovorder.archive.upload") as upload:
                self.assertTrue(self.archive.update_meta(self.path, True))
        self.assertEqual(modify.call_args.args[0], "in.gov.andhra.goir.2025-06-01.1-12345")
        self.assertEqual(upload.call_args.args[1], [self.storage.get_metafile_path(self.path)])

    def test_failed_upload_returns_nonzero_from_command(self):
        with patch("egovorder.archive.OrderIA", return_value=self.archive):
            with patch.object(self.archive, "upload", return_value=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = iasync.main([
                        "-D", self.temp.name, "-u", "-a", "fake-access", "-k", "fake-secret",
                    ])
        self.assertEqual(result, 1)

    def test_explicit_legacy_source_is_rejected_before_archive_access(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            iasync.main(["-D", self.temp.name, "-u", "-r", "central_weekly/2025-06-01/1",
                         "-a", "fake-access", "-k", "fake-secret"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
