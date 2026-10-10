import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wa_parser import detect_date_order, message_hash, parse_chat_text  # noqa: E402

SAMPLE = """\ufeff6/10/26, 12:49 PM - Messages and calls are end-to-end encrypted. Only people in this chat can read, listen to, or share them. *Learn more*
6/10/26, 12:49 PM - TANTI KUSAENI AMG created group "PROJECT TEAM"
6/10/26, 12:50 PM - You're now an admin
6/10/26, 12:51 PM - Nurul Latifah Khoiriyah AMG added Naufal Nashshar Lathif AMG
6/10/26, 1:33 PM - TANTI KUSAENI AMG: IMG-20260610-WA0013.jpg (file attached)
6/10/26, 1:34 PM - TANTI KUSAENI AMG: itu data dari pak jo yg dibuat pak dedi
6/10/26, 2:52 PM - TANTI KUSAENI AMG: <Media omitted>
AMG-SAKURA EXTENTION-PENGAJUAN ZONA-R2-SAKURA EXT-_260607_170027.pdf
6/10/26, 3:05 PM - Tri Aji Prabandaru: IMG-20260610-WA0014.jpg (file attached)
sementara baru ini, tapi kita cek lapangan juga,
biasanya suka ada kerja tambah
6/10/26, 3:10 PM - TANTI KUSAENI AMG: <Media omitted>
Zahratun FB-01
6/13/26, 11:33 AM - TANTI KUSAENI AMG: selamat siang Pak @\u2068Dedy AMG\u2069 cek https://app.notion.com/p/Laporan-W2. ya <This message was edited>
6/13/26, 11:34 AM - Tri Aji Prabandaru: 
6/15/26, 8:16 AM - JO NUM added +62 816-912-943
6/15/26, 8:17 AM - +62 816-912-943: halo semua
9/5/26, 10:42 AM - Tri Aji Prabandaru: You deleted this message
9/30/26, 9:34 AM - TANTI KUSAENI AMG: ok
9/30/26, 9:35 AM - TANTI KUSAENI AMG: ok
"""


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.chat = parse_chat_text(SAMPLE, "PROJECT TEAM")
        self.msgs = self.chat.messages

    def test_date_order_detected_from_day_over_12(self):
        self.assertEqual(self.chat.date_order, "MDY")
        self.assertEqual(self.msgs[0].ts.strftime("%Y-%m-%d %H:%M"), "2026-06-10 12:49")

    def test_pm_and_midnight_conversion(self):
        c = parse_chat_text("6/10/26, 12:05 AM - A: x\n6/10/26, 1:30 PM - A: y\n6/30/26, 12:00 PM - A: z")
        self.assertEqual([m.ts.hour for m in c.messages], [0, 13, 12])

    def test_narrow_nbsp_before_ampm(self):
        c = parse_chat_text("6/10/26, 12:49\u202fPM - A: hi\n6/30/26, 1:00\u202fPM - A: ok")
        self.assertEqual(c.messages[0].ts.hour, 12)
        self.assertEqual(c.messages[0].body, "hi")

    def test_system_messages(self):
        kinds = [(m.kind, m.system_event) for m in self.msgs[:4]]
        self.assertEqual(kinds, [("system", "encryption"), ("system", "created"),
                                 ("system", "admin"), ("system", "added")])

    def test_system_line_with_colon_in_subject(self):
        c = parse_chat_text('6/10/26, 1:00 PM - Ani changed the subject from "Tim: A" to "Tim: B"\n6/30/26, 1:00 PM - Ani: hi')
        self.assertEqual(c.messages[0].kind, "system")
        self.assertEqual(c.messages[0].system_event, "subject")
        self.assertIsNone(c.messages[0].sender)

    def test_attachment_with_file_attached_marker(self):
        m = self.msgs[4]
        self.assertEqual(m.kind, "media")
        self.assertEqual(m.attachments[0].filename, "IMG-20260610-WA0013.jpg")
        self.assertEqual(m.attachments[0].media_kind, "image")
        self.assertEqual(m.body, "")

    def test_media_omitted_with_filename_on_next_line(self):
        m = self.msgs[6]
        self.assertEqual(m.kind, "media")
        self.assertTrue(m.attachments[0].filename.endswith(".pdf"))
        self.assertEqual(m.attachments[0].media_kind, "document")
        self.assertEqual(m.body, "")

    def test_media_omitted_followed_by_caption_not_filename(self):
        m = self.msgs[8]
        self.assertEqual(m.kind, "media")
        self.assertIsNone(m.attachments[0].filename)
        self.assertEqual(m.body, "Zahratun FB-01")

    def test_multiline_caption(self):
        m = self.msgs[7]
        self.assertEqual(m.body, "sementara baru ini, tapi kita cek lapangan juga,\nbiasanya suka ada kerja tambah")

    def test_mentions_links_and_edited(self):
        m = self.msgs[9]
        self.assertEqual(m.mentions, ["Dedy AMG"])
        self.assertTrue(m.edited)
        self.assertNotIn("\u2068", m.body)
        self.assertEqual(m.links[0]["url"], "https://app.notion.com/p/Laporan-W2")  # titik penutup kalimat dibuang
        self.assertEqual(m.links[0]["domain"], "app.notion.com")

    def test_empty_body_after_sender(self):
        m = self.msgs[10]
        self.assertEqual((m.sender, m.kind, m.body), ("Tri Aji Prabandaru", "text", ""))

    def test_phone_sender_and_phone_in_system_line(self):
        self.assertEqual(self.msgs[11].kind, "system")
        m = self.msgs[12]
        self.assertEqual(m.sender, "+62 816-912-943")
        self.assertEqual(m.sender_phone, "62816912943")

    def test_deleted_message(self):
        m = self.msgs[13]
        self.assertEqual((m.kind, m.body), ("deleted", ""))

    def test_same_text_different_minute_gets_distinct_hashes(self):
        a, b = self.msgs[-2], self.msgs[-1]
        self.assertNotEqual(message_hash("g", a), message_hash("g", b))

    def test_same_second_duplicates_get_occurrence_counter(self):
        c = parse_chat_text("6/10/26, 1:00 PM - A: ok\n6/10/26, 1:00 PM - A: ok\n6/30/26, 1:00 PM - A: x")
        hashes = {message_hash("g", m) for m in c.messages}
        self.assertEqual([m.occurrence for m in c.messages[:2]], [0, 1])
        self.assertEqual(len(hashes), 3)

    def test_reimport_is_idempotent_and_overlap_dedupes(self):
        first = parse_chat_text(SAMPLE)
        h1 = {message_hash("g", m) for m in first.messages}
        again = {message_hash("g", m) for m in parse_chat_text(SAMPLE).messages}
        self.assertEqual(h1, again)
        longer = SAMPLE + "10/3/26, 11:58 AM - TANTI KUSAENI AMG: pesan baru\n"
        h2 = {message_hash("g", m) for m in parse_chat_text(longer).messages}
        self.assertEqual(len(h2 - h1), 1)
        self.assertTrue(h1 <= h2)

    def test_group_key_changes_hash(self):
        m = self.msgs[5]
        self.assertNotEqual(message_hash("g1", m), message_hash("g2", m))


class DateOrderTests(unittest.TestCase):
    @staticmethod
    def _entries(dates):
        return [(a, b, 26, 9, 0, 0, None) for a, b in dates]

    def test_dmy_when_first_component_over_12(self):
        self.assertEqual(detect_date_order(self._entries([(28, 1), (29, 1)]))[0], "DMY")

    def test_ambiguous_resolved_by_chronology(self):
        order, reason = detect_date_order(self._entries([(3, 4), (3, 5), (10, 5), (2, 6)]))
        self.assertEqual(order, "DMY")
        self.assertIn("ambigu", reason)

    def test_truly_ambiguous_warns(self):
        c = parse_chat_text("05/01/26, 09.00 - A: x\n05/02/26, 09.00 - A: y")
        self.assertTrue(any("AMBIGU" in w for w in c.warnings))

    def test_manual_override(self):
        c = parse_chat_text("05/01/26, 09.00 - A: x", date_order="DMY")
        self.assertEqual(c.messages[0].ts.month, 1)

    def test_24h_dotted_time(self):
        c = parse_chat_text("28/01/2026, 14.05 - A: halo")
        self.assertEqual((c.date_order, c.messages[0].ts.hour, c.messages[0].ts.minute), ("DMY", 14, 5))


if __name__ == "__main__":
    unittest.main()
