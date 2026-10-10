import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import wa_loader  # noqa: E402
from test_wa_parser import SAMPLE  # noqa: E402
from wa_parser import message_to_dict, parse_chat_text  # noqa: E402


def build_jsonl_rows(text=SAMPLE):
    chat = parse_chat_text(text, "PROJECT TEAM")
    header = {"group_name": "PROJECT TEAM", "group_key": "PROJECT TEAM", "date_order": chat.date_order,
              "tz": "Asia/Jakarta", "warnings": chat.warnings}
    return header, [message_to_dict(m, "PROJECT TEAM") for m in chat.messages]


class FakeCursor:
    """Meniru kursor psycopg: memvalidasi arity SQL dan menyimpan state sederhana."""

    def __init__(self, existing_hashes=(), pending_attachments=(), known_aliases=None, sha_paths=None):
        self.log, self._result = [], None
        self.existing_hashes = list(existing_hashes)
        self.pending_attachments = list(pending_attachments)
        self.known_aliases = dict(known_aliases or {})
        self.sha_paths = dict(sha_paths or {})
        self.inserted = {}

    def _check(self, sql, params):
        n = sql.count("%s")
        assert n == len(params), f"placeholder {n} != params {len(params)} :: {sql[:90]}"

    def execute(self, sql, params=()):
        self._check(sql, params)
        low = " ".join(sql.lower().split())
        self.log.append(low)
        self._result = None
        if low.startswith("insert into"):
            table = low.split()[2]
            self.inserted.setdefault(table, []).append(params)
            if "returning id" in low:
                self._result = [(uuid.uuid4(),)]
        elif "from workspaces" in low:
            self._result = [(uuid.uuid4(),)]
        elif "from projects" in low:
            self._result = []
        elif "from participant_aliases" in low:
            self._result = list(self.known_aliases.items())
        elif "select msg_hash from messages" in low:
            self._result = [(h,) for h in self.existing_hashes]
        elif "from attachments a join messages" in low:
            self._result = list(self.pending_attachments)
        elif "select storage_path from attachments where sha256" in low:
            p = self.sha_paths.get(params[0])
            self._result = [(p,)] if p else []
        elif low.startswith("update attachments"):
            self.inserted.setdefault("attachments_update", []).append(params)

    def executemany(self, sql, rows):
        for params in rows:
            self.execute(sql, params)

    def fetchone(self):
        return (self._result or [None])[0]

    def fetchall(self):
        return list(self._result or [])


class RunLoadTests(unittest.TestCase):
    def setUp(self):
        self.header, self.msgs = build_jsonl_rows()
        self.ws = uuid.uuid4()

    def test_first_import_inserts_everything(self):
        cur = FakeCursor()
        s = wa_loader.run_load(cur, self.ws, self.header, self.msgs, "AMG", "chat.jsonl")
        self.assertEqual((s["total"], s["new"], s["duplicates"]), (len(self.msgs), len(self.msgs), 0))
        self.assertEqual(len(cur.inserted["messages"]), len(self.msgs))
        self.assertIn("project_groups", cur.inserted)
        senders = {m["sender"] for m in self.msgs if m["sender"]}
        self.assertEqual(s["new_people"], len(senders))
        self.assertEqual(len(cur.inserted["participant_aliases"]), len(senders))

    def test_reimport_skips_existing_messages(self):
        known = {m["msg_hash"] for m in self.msgs[:10]}
        cur = FakeCursor(existing_hashes=known)
        s = wa_loader.run_load(cur, self.ws, self.header, self.msgs)
        self.assertEqual((s["new"], s["duplicates"]), (len(self.msgs) - 10, 10))
        self.assertEqual(len(cur.inserted["messages"]), len(self.msgs) - 10)

    def test_known_people_not_recreated(self):
        senders = {m["sender"]: uuid.uuid4() for m in self.msgs if m["sender"]}
        cur = FakeCursor(known_aliases=senders)
        s = wa_loader.run_load(cur, self.ws, self.header, self.msgs)
        self.assertEqual(s["new_people"], 0)
        self.assertNotIn("participants", cur.inserted)

    def test_attachments_and_links_follow_new_messages_only(self):
        n_att = sum(len(m["attachments"]) for m in self.msgs)
        n_link = sum(len(m["links"]) for m in self.msgs)
        cur = FakeCursor()
        s = wa_loader.run_load(cur, self.ws, self.header, self.msgs)
        self.assertEqual((s["attachments"], s["links"]), (n_att, n_link))
        cur2 = FakeCursor(existing_hashes={m["msg_hash"] for m in self.msgs})
        s2 = wa_loader.run_load(cur2, self.ws, self.header, self.msgs)
        self.assertEqual((s2["new"], s2["attachments"], s2["links"]), (0, 0, 0))

    def test_seq_comes_from_src_line_and_is_last_param(self):
        cur = FakeCursor()
        wa_loader.run_load(cur, self.ws, self.header, self.msgs)
        seqs = [row[-1] for row in cur.inserted["messages"]]
        self.assertEqual(seqs, [m["src_line"] for m in self.msgs])

    def test_system_messages_have_no_participant(self):
        cur = FakeCursor()
        wa_loader.run_load(cur, self.ws, self.header, self.msgs)
        for row, m in zip(cur.inserted["messages"], self.msgs):
            if m["kind"] == "system":
                self.assertIsNone(row[6])  # participant_id
                self.assertIsNone(row[5])  # sender_alias

    def test_nul_bytes_are_stripped(self):
        header, msgs = build_jsonl_rows("6/30/26, 1:00 PM - A: halo\x00dunia")
        cur = FakeCursor()
        wa_loader.run_load(cur, self.ws, header, msgs)
        self.assertEqual(cur.inserted["messages"][0][9], "halodunia")

    def test_empty_file(self):
        cur = FakeCursor()
        s = wa_loader.run_load(cur, self.ws, self.header, [])
        self.assertEqual((s["total"], s["new"]), (0, 0))


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.jpg").write_bytes(b"AAA")
        (self.dir / "big.mp4").write_bytes(b"x" * (2 * 1024 * 1024))
        self.calls = []
        import wa_drive
        self._orig = wa_drive.upload_to_drive
        def _mock_upload(path, filename, group_name, project_name="Default"):
            self.calls.append((filename, group_name, project_name))
            return ("drive_id_123", "https://view.url")
        wa_drive.upload_to_drive = _mock_upload

    def tearDown(self):
        import wa_drive
        wa_drive.upload_to_drive = self._orig
        self.tmp.cleanup()

    def test_upload_skip_missing_big_and_reuse(self):
        gid = uuid.uuid4()
        pend = [(uuid.uuid4(), "a.jpg"), (uuid.uuid4(), "hilang.pdf"), (uuid.uuid4(), "big.mp4")]
        cur = FakeCursor(pending_attachments=pend)
        st = wa_loader.upload_pending(cur, gid, "Test Group", "Test Project", self.dir, max_mb=1)
        self.assertEqual((st["uploaded"], st["missing"], st["too_big"]), (1, 1, 1))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "a.jpg")
        self.assertEqual(len(cur.inserted["attachments_update"]), 1)

        import hashlib
        sha = hashlib.sha256(b"AAA").hexdigest()
        cur2 = FakeCursor(pending_attachments=[pend[0]], sha_paths={sha: "lama_drive_id"})
        self.calls.clear()
        st2 = wa_loader.upload_pending(cur2, gid, "Test Group", "Test Project", self.dir, max_mb=1)
        self.assertEqual((st2["reused"], st2["uploaded"], len(self.calls)), (1, 0, 0))


class UploadHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        import wa_drive
        self._orig = wa_drive.upload_to_drive
        self.uploaded = []

        def _mock_upload(path, filename, group_name, project_name="Default"):
            self.uploaded.append(filename)
            return (f"id_{filename}", "https://view.url")
        wa_drive.upload_to_drive = _mock_upload

    def tearDown(self):
        import wa_drive
        wa_drive.upload_to_drive = self._orig
        self.tmp.cleanup()

    def test_file_sha256_reads_in_chunks_and_matches_hashlib(self):
        import hashlib
        data = bytes(range(256)) * 50
        p = self.dir / "x.bin"
        p.write_bytes(data)
        self.assertEqual(wa_loader.file_sha256(p, chunk=7), hashlib.sha256(data).hexdigest())
        self.assertEqual(wa_loader.file_sha256(p), hashlib.sha256(data).hexdigest())

    def test_commit_called_after_every_file(self):
        (self.dir / "a.jpg").write_bytes(b"AAA")
        (self.dir / "b.jpg").write_bytes(b"BBB")
        pend = [(uuid.uuid4(), "a.jpg"), (uuid.uuid4(), "hilang.pdf"), (uuid.uuid4(), "b.jpg")]
        commits = []
        cur = FakeCursor(pending_attachments=pend)
        st = wa_loader.upload_pending(cur, uuid.uuid4(), "G", "P", self.dir, max_mb=1,
                                      commit=lambda: commits.append(1))
        self.assertEqual((st["uploaded"], st["missing"]), (2, 1))
        self.assertEqual(len(commits), 2)  # satu per file yang tercatat; file hilang tidak memicu commit

    def test_failed_upload_does_not_commit_and_continues(self):
        import wa_drive
        (self.dir / "a.jpg").write_bytes(b"AAA")
        (self.dir / "b.jpg").write_bytes(b"BBB")

        def flaky(path, filename, group_name, project_name="Default"):
            if filename == "a.jpg":
                raise OSError("jaringan putus")
            return ("id_b", "https://view.url")
        wa_drive.upload_to_drive = flaky

        commits = []
        pend = [(uuid.uuid4(), "a.jpg"), (uuid.uuid4(), "b.jpg")]
        st = wa_loader.upload_pending(FakeCursor(pending_attachments=pend), uuid.uuid4(), "G", "P",
                                      self.dir, max_mb=1, commit=lambda: commits.append(1))
        self.assertEqual((st["failed"], st["uploaded"], len(commits)), (1, 1, 1))


class EnvTests(unittest.TestCase):
    def test_load_env_parses_quotes_and_comments(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# komentar\nTEST_A="abc"\nTEST_B=def\n\nbad line\n', encoding="utf-8")
            os.environ.pop("TEST_A", None); os.environ.pop("TEST_B", None)
            wa_loader.load_env(str(p))
            self.assertEqual((os.environ["TEST_A"], os.environ["TEST_B"]), ("abc", "def"))


if __name__ == "__main__":
    unittest.main()
