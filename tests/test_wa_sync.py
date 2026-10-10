import contextlib
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import wa_sync  # noqa: E402
from test_wa_loader import FakeCursor  # noqa: E402
from test_wa_parser import SAMPLE  # noqa: E402

CHAT_TXT = "WhatsApp Chat with PROJECT TEAM.txt"
AMBIGUOUS = "05/01/26, 09.00 - A: x\n05/02/26, 09.00 - A: y\n"


class FakeConn:
    def __init__(self, cur=None):
        self.cur = cur or FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return contextlib.nullcontext(self.cur)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def make_export(parent: Path, name="WhatsApp Chat with PROJECT TEAM", text=SAMPLE, txt_name=CHAT_TXT) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / txt_name).write_text(text, encoding="utf-8")
    return d


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_folders_exports_and_ignored_entries(self):
        make_export(self.inbox / "AMG", "WhatsApp Chat with A")
        (self.inbox / "AMG" / "b.zip").write_bytes(b"x")
        (self.inbox / "AMG" / "catatan.md").write_text("bukan ekspor")
        make_export(self.inbox / "Rizma", "WhatsApp Chat with C")
        make_export(self.inbox / "_done" / "AMG", "sudah selesai")
        (self.inbox / ".sync.lock").write_text("1")

        found, orphans = wa_sync.discover_exports(self.inbox)
        got = sorted((p.name, proj) for p, proj in found)
        self.assertEqual(got, [("WhatsApp Chat with A", "AMG"), ("WhatsApp Chat with C", "Rizma"),
                               ("b.zip", "AMG")])
        self.assertEqual(orphans, [])

    def test_export_at_root_needs_project(self):
        make_export(self.inbox, "WhatsApp Chat with X")
        found, orphans = wa_sync.discover_exports(self.inbox)
        self.assertEqual((found, [o.name for o in orphans]), ([], ["WhatsApp Chat with X"]))

        found, orphans = wa_sync.discover_exports(self.inbox, default_project="Default")
        self.assertEqual([(p.name, proj) for p, proj in found], [("WhatsApp Chat with X", "Default")])
        self.assertEqual(orphans, [])


class ProcessExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = uuid.uuid4()

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_import_records_export_name_and_commits(self):
        path = make_export(self.root)
        conn = FakeConn()
        r = wa_sync.process_export(conn, self.ws, path, "AMG", wa_sync.Options(drive=False))
        self.assertEqual(r["status"], "imported")
        self.assertEqual(r["group"], "PROJECT TEAM")
        self.assertEqual(r["stats"]["new"], r["stats"]["total"])
        self.assertGreater(conn.commits, 0)
        # kolom filename pada tabel imports berisi nama ekspor asli, bukan "chat.jsonl"
        self.assertEqual(conn.cur.inserted["imports"][0][1], path.name)

    def test_zip_import(self):
        zpath = self.root / "WhatsApp Chat with PROJECT TEAM.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(CHAT_TXT, SAMPLE)
            zf.writestr("IMG-20260610-WA0013.jpg", b"jpgdata")
        conn = FakeConn()
        r = wa_sync.process_export(conn, self.ws, zpath, "AMG", wa_sync.Options(drive=False))
        self.assertEqual(r["status"], "imported")
        self.assertTrue(zpath.exists())  # pengarsipan dilakukan run_sync, bukan process_export

    def test_zip_with_wrapper_folder(self):
        zpath = self.root / "export.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("WhatsApp Chat with PROJECT TEAM/" + CHAT_TXT, SAMPLE)
        r = wa_sync.process_export(FakeConn(), self.ws, zpath, "AMG", wa_sync.Options(drive=False))
        self.assertEqual(r["status"], "imported")

    def test_dry_run_rolls_back_and_never_commits(self):
        path = make_export(self.root)
        conn = FakeConn()
        r = wa_sync.process_export(conn, self.ws, path, "AMG", wa_sync.Options(drive=False, dry_run=True))
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual((conn.commits, conn.rollbacks), (0, 1))

    def test_ambiguous_date_order_is_skipped_not_guessed(self):
        path = make_export(self.root, text=AMBIGUOUS)
        conn = FakeConn()
        r = wa_sync.process_export(conn, self.ws, path, "AMG", wa_sync.Options(drive=False))
        self.assertEqual(r["status"], "needs_attention")
        self.assertIn("--date-order", r["error"])
        self.assertEqual(conn.cur.inserted, {})
        self.assertEqual(conn.commits, 0)

    def test_manual_date_order_overrides_ambiguity(self):
        path = make_export(self.root, text=AMBIGUOUS)
        r = wa_sync.process_export(FakeConn(), self.ws, path, "AMG",
                                   wa_sync.Options(drive=False, date_order="DMY"))
        self.assertEqual(r["status"], "imported")

    def test_chat_without_messages_fails(self):
        path = make_export(self.root, text="halo ini bukan ekspor WhatsApp")
        r = wa_sync.process_export(FakeConn(), self.ws, path, "AMG", wa_sync.Options(drive=False))
        self.assertEqual(r["status"], "failed")


class RunSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name)
        self.done = self.inbox / "_done"
        self.ws = uuid.uuid4()

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_is_archived_and_failure_stays(self):
        good = make_export(self.inbox / "AMG", "WhatsApp Chat with PROJECT TEAM")
        bad = make_export(self.inbox / "AMG", "WhatsApp Chat with Rusak", text="bukan chat",
                          txt_name="WhatsApp Chat with Rusak.txt")
        exports, _ = wa_sync.discover_exports(self.inbox)
        results = wa_sync.run_sync(FakeConn(), self.ws, exports, wa_sync.Options(drive=False), self.done)

        status = {r["path"].name: r["status"] for r in results}
        self.assertEqual(status, {good.name: "imported", bad.name: "failed"})
        self.assertFalse(good.exists())
        self.assertTrue(bad.exists())
        archived = list((self.done / "AMG").iterdir())
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0].name.endswith(good.name))

    def test_dry_run_and_keep_leave_inbox_untouched(self):
        path = make_export(self.inbox / "AMG")
        exports, _ = wa_sync.discover_exports(self.inbox)
        wa_sync.run_sync(FakeConn(), self.ws, exports, wa_sync.Options(drive=False, dry_run=True), self.done)
        self.assertTrue(path.exists())
        wa_sync.run_sync(FakeConn(), self.ws, exports, wa_sync.Options(drive=False), self.done, keep=True)
        self.assertTrue(path.exists())
        self.assertFalse(self.done.exists())

    def test_exception_in_one_export_does_not_stop_others(self):
        make_export(self.inbox / "AMG", "WhatsApp Chat with A", txt_name="WhatsApp Chat with A.txt")
        make_export(self.inbox / "AMG", "WhatsApp Chat with B", txt_name="WhatsApp Chat with B.txt")
        exports, _ = wa_sync.discover_exports(self.inbox)

        calls = []
        original = wa_sync.process_export

        def flaky(conn, ws, path, project, opts):
            calls.append(path.name)
            if len(calls) == 1:
                raise RuntimeError("koneksi putus")
            return original(conn, ws, path, project, opts)

        wa_sync.process_export = flaky
        try:
            results = wa_sync.run_sync(FakeConn(), self.ws, exports, wa_sync.Options(drive=False), self.done)
        finally:
            wa_sync.process_export = original
        self.assertEqual([r["status"] for r in results], ["failed", "imported"])
        self.assertIn("koneksi putus", results[0]["error"])


class LockTests(unittest.TestCase):
    def test_lock_blocks_second_run_and_is_released(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d)
            with wa_sync.sync_lock(inbox):
                self.assertTrue((inbox / wa_sync.LOCK_NAME).exists())
                with self.assertRaises(SystemExit):
                    with wa_sync.sync_lock(inbox):
                        pass
            self.assertFalse((inbox / wa_sync.LOCK_NAME).exists())
            with wa_sync.sync_lock(inbox):  # bisa diambil lagi setelah dilepas
                pass


if __name__ == "__main__":
    unittest.main()
