# -*- coding: utf-8 -*-
"""Tests for sms_import.py. Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import base64
import collections
import contextlib
import glob
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sms_import as si  # noqa: E402

ASSETS = os.path.join(HERE, "assets")
SAMPLES = os.path.join(ASSETS, "samples.xml")
CALLS = os.path.join(ASSETS, "calls.xml")
SCHEMA = os.path.join(ASSETS, "commhistory_schema_v4.sql")
RIL0 = si.RING_ACCOUNT + "ril_0"
RIL1 = si.RING_ACCOUNT + "ril_1"
IMSI = "262019876543210"
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAD0lEQVR42mNgqLiOQMRxABNCFPFmAF/zAAAAAElFTkSuQmCC")


def create_db(directory):
    """An empty commhistory.db with the schema of a Sailfish OS 5.2 phone."""
    path = os.path.join(directory, "commhistory.db")
    conn = sqlite3.connect(path)
    with open(SCHEMA, encoding="utf-8") as schema:
        conn.executescript(schema.read())
    conn.close()
    return path


def add_native_sms(conn, remote_uid, start, local_uid=RIL1):
    """Simulate a conversation that already exists on the phone."""
    group_id = conn.execute("INSERT INTO Groups (localUid, remoteUids, type, chatName, lastModified)"
                            " VALUES (?, ?, 0, NULL, 0)", (local_uid, remote_uid)).lastrowid
    event_id = conn.execute(
        "INSERT INTO Events (type, startTime, endTime, direction, isDraft, isRead, isMissedCall,"
        " isEmergencyCall, status, bytesReceived, localUid, remoteUid, freeText, groupId,"
        " messageToken, lastModified, reportDelivery, validityPeriod, readStatus, reportRead,"
        " reportedReadRequested, isAction) VALUES (2, ?, ?, 1, 0, 1, 0, 0, 0, 0, ?, ?, 'native',"
        " ?, 'token', 0, 0, 0, 0, 0, 0, 0)",
        (start, start, local_uid, remote_uid, group_id)).lastrowid
    conn.execute("INSERT INTO EventProperties (eventId, key, value) VALUES (?, 'subscriberIdentity', ?)",
                 (event_id, IMSI))
    return group_id


class MinimizePhoneNumberTests(unittest.TestCase):
    def test_international_and_national_forms_match(self):
        for number in ("+4915112345678", "015112345678", "+49 (151) 123-456 78"):
            self.assertEqual(si.minimize_phone_number(number), "12345678")

    def test_short_numbers_and_sender_names(self):
        self.assertEqual(si.minimize_phone_number("22000"), "22000")
        self.assertEqual(si.minimize_remote_uid("DHL Paket"), "dhl paket")

    def test_dial_string_is_kept(self):
        self.assertEqual(si.minimize_phone_number("+4930123456,123"), "30123456p123")

    def test_unicode_digits(self):
        arabic = "".join(chr(0x0660 + int(d)) for d in "015112345678")
        self.assertEqual(si.minimize_phone_number(arabic), "12345678")


class RepairXmlTests(unittest.TestCase):
    def test_surrogate_pairs_are_joined(self):
        self.assertEqual(si.repair_xml_line(b'body="a &#55357;&#56832; b"'), b'body="a &#128512; b"')
        self.assertEqual(si.repair_xml_line(b"&#xD83D;&#xDE00;"), b"&#128512;")

    def test_broken_references(self):
        self.assertEqual(si.repair_xml_line(b"&#55357;x&#56832;&#55357;"), b"&#65533;x&#65533;&#65533;")
        self.assertEqual(si.repair_xml_line(b"a&#1;b&#65535;c"), b"abc")

    def test_valid_content_is_untouched(self):
        line = b'<sms body="Gr&#252;&#223;e&#10;&amp; &#x1F600;" />\n'
        self.assertEqual(si.repair_xml_line(line), line)

    def test_raw_control_characters_are_removed(self):
        self.assertEqual(si.repair_xml_line(b"a\x01b\tc\r\n"), b"ab\tc\r\n")


class ReadBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skipped = collections.Counter()
        cls.messages = si.read_backup(SAMPLES, skipped=cls.skipped)
        cls.calls = si.read_backup(CALLS, skipped=cls.skipped)

    def find(self, events, **fields):
        found = [e for e in events if all(getattr(e, k) == v for k, v in fields.items())]
        self.assertEqual(len(found), 1, fields)
        return found[0]

    def test_counts_and_skipped_entries(self):
        self.assertEqual(collections.Counter(e.kind for e in self.messages), {"sms": 6, "mms": 2})
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.skipped, {"drafts/unsent SMS": 1, "MMS that were never downloaded": 1,
                                        "calls of unknown type": 1})

    def test_sent_and_received_sms(self):
        sent = self.find(self.messages, free_text="Hi Test!")
        self.assertEqual((sent.type, sent.direction, sent.status, sent.start, sent.end),
                         (si.TYPE_SMS, si.OUTBOUND, si.STATUS_SENT, 1341324666, 1341324666))
        self.assertEqual(sent.remote_uid, "+4949494949499")
        received = self.find(self.messages, free_text="Bye Tester! :)", remote_uid="+4949494949499")
        self.assertEqual((received.direction, received.status, received.start, received.is_read),
                         (si.INBOUND, si.STATUS_UNKNOWN, 1341324992, True))

    def test_emoji_newlines_and_control_characters(self):
        self.find(self.messages, remote_uid="049494949499", free_text="Schöne Grüße \U0001F600\nzweite Zeile")
        self.find(self.messages, remote_uid="DHL", free_text="Ihr Paket kommt heute")

    def test_unread_flag_is_only_kept_on_request(self):
        self.assertTrue(self.find(self.messages, remote_uid="049494949499").is_read)
        kept = si.read_backup(SAMPLES, keep_unread=True)
        self.assertFalse(self.find(kept, remote_uid="049494949499").is_read)

    def test_received_mms(self):
        mms = self.find(self.messages, kind="mms", direction=si.INBOUND)
        self.assertEqual((mms.type, mms.status, mms.start, mms.remote_uid, mms.subject),
                         (si.TYPE_MMS, si.STATUS_RECEIVED, 1341328000, "+4948186548949", None))
        self.assertEqual(mms.free_text, "Ein Bild für dich")
        self.assertEqual(mms.headers, "x-mms-to\x1d+4949494949499\x1e+4911111111111")
        self.assertEqual([p[:3] for p in mms.parts], [
            ("smil", "application/smil", "smil.xml"),
            ("pixel", "image/png", "pixel.png"),
            ("text_0", "text/plain;charset=utf-8", "text_0.txt")])
        self.assertTrue(mms.parts[0][3].startswith(b"<smil><body>"))
        self.assertEqual(mms.parts[1][3], PIXEL)
        self.assertEqual(mms.parts[2][3], "Ein Bild für dich".encode("utf-8"))

    def test_sent_group_mms_goes_to_first_recipient(self):
        mms = self.find(self.messages, kind="mms", direction=si.OUTBOUND)
        self.assertEqual((mms.status, mms.remote_uid, mms.subject, mms.free_text),
                         (si.STATUS_SENT, "+4911111111111", "Gruppe", "Hallo zusammen"))
        self.assertEqual(mms.headers, "x-mms-to\x1d+4911111111111\x1e+4948186548949")

    def test_calls(self):
        summary = [(c.start, c.direction, c.is_missed, c.end - c.start, c.remote_uid, c.properties)
                   for c in self.calls]
        self.assertEqual(summary, [
            (1500000000, si.OUTBOUND, False, 2, "+4930303030300", {}),
            (1500003600, si.INBOUND, False, 278, "+4989898989800", {}),
            (1500007200, si.INBOUND, True, 0, "+4930303030300", {}),
            (1500010800, si.INBOUND, True, 0, "+4930303030300", {"incomingStatus": "rejected"}),
            (1500014400, si.INBOUND, True, 0, "+4930303030300", {"incomingStatus": "rejected"}),
            (1500018000, si.INBOUND, True, 0, "", {}),     # withheld number
            (1500021600, si.INBOUND, False, 31, "", {}),   # unknown number
            (1500025200, si.INBOUND, True, 0, "+4930303030300", {}),  # voicemail
        ])

    def test_invalid_file(self):
        path = os.path.join(tempfile.mkdtemp(), "broken.xml")
        try:
            with open(path, "w") as broken:
                broken.write("<smses><sms ")
            with self.assertRaises(si.UserError):
                si.read_backup(path)
        finally:
            shutil.rmtree(os.path.dirname(path))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = create_db(self.tmp)
        self.data_dir = os.path.join(self.tmp, "data")
        self.backup_dir = os.path.join(self.tmp, "backups")
        conn = sqlite3.connect(self.db)
        self.native_group = add_native_sms(conn, "+4949494949499", 1790000000)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args):
        # --no-restart: never touch the real services when the tests run on a phone
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = si.main(list(args) + ["--db", self.db, "--backup-dir", self.backup_dir,
                                         "--no-restart"])
        return code, output.getvalue()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.tmp)
                      for d, _, names in os.walk(self.data_dir) for f in names)

    def test_import(self):
        code, output = self.run_cli("--import", SAMPLES, CALLS, "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("Imported 6 SMS, 2 MMS (1 attachment) and 8 calls, 3 new conversations.", output)
        self.assertEqual(self.query("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(len(glob.glob(os.path.join(self.backup_dir, "commhistory-*.db"))), 1)

        # Same contact in national format joins the existing conversation; the
        # new conversations are named like the phone would name them.
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events WHERE groupId = ?", (self.native_group,)),
                         [(4,)])
        self.assertEqual(self.query("SELECT remoteUids FROM Groups WHERE id > ? ORDER BY id", (self.native_group,)),
                         [("+4948186548949",), ("DHL",), ("+4911111111111",)])
        self.assertEqual(self.query("SELECT DISTINCT localUid FROM Events"), [(RIL1,)])
        self.assertEqual(self.query("SELECT DISTINCT localUid FROM Groups"), [(RIL1,)])

        # Every event carries the SIM identity like native ones; calls have no group.
        self.assertEqual(self.query("SELECT key, value, COUNT(*) FROM EventProperties GROUP BY key, value"),
                         [("incomingStatus", "rejected", 2), ("subscriberIdentity", IMSI, 17)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events WHERE hasExtraProperties != 1"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events WHERE type = 3 AND groupId IS NOT NULL"),
                         [(0,)])
        self.assertEqual(self.query("SELECT remoteUid, isMissedCall, endTime - startTime FROM Events"
                                    " WHERE type = 3 AND remoteUid = '' ORDER BY startTime"),
                         [("", 1, 0), ("", 0, 31)])
        self.assertEqual(self.query("SELECT typeof(startTime), typeof(lastModified), COUNT(*) FROM Events"
                                    " GROUP BY 1, 2"), [("integer", "integer", 17)])
        tokens = self.query("SELECT direction, messageToken FROM Events WHERE type = 2 AND id > 1")
        self.assertTrue(all(t.startswith("/ril_1/message_") and len(t) == 55 for d, t in tokens if d == 2))
        self.assertTrue(all(len(t) == 36 for d, t in tokens if d == 1))

    def test_mms_parts_are_stored_like_commhistoryd_does(self):
        self.assertEqual(self.run_cli("--import", SAMPLES, "--yes")[0], 0)
        (received,), (sent,) = self.query("SELECT id FROM Events WHERE type = 6 ORDER BY direction")
        parts = self.query("SELECT eventId, contentId, contentType, path FROM MessageParts ORDER BY id")
        directory = os.path.join(self.data_dir, str(received))
        self.assertEqual(parts, [
            (received, "smil", "application/smil", os.path.join(directory, "smil.xml")),
            (received, "pixel", "image/png", os.path.join(directory, "pixel.png")),
            (received, "text_0", "text/plain;charset=utf-8", os.path.join(directory, "text_0.txt")),
            (sent, "text_0", "text/plain;charset=utf-8", os.path.join(self.data_dir, str(sent), "text_0.txt"))])
        with open(parts[1][3], "rb") as image:
            self.assertEqual(image.read(), PIXEL)
        with open(parts[2][3], encoding="utf-8") as text:
            self.assertEqual(text.read(), "Ein Bild für dich")
        self.assertEqual(self.query("SELECT hasMessageParts, status, freeText FROM Events WHERE type = 6"
                                    " ORDER BY direction"),
                         [(1, -1, "Ein Bild für dich"), (1, 2, "Hallo zusammen")])

    def test_second_import_skips_everything(self):
        self.assertEqual(self.run_cli("--import", SAMPLES, CALLS, "--yes")[0], 0)
        files = self.files()
        code, output = self.run_cli("--import", CALLS, SAMPLES, "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("Imported 0 SMS, 0 MMS (0 attachments) and 0 calls, 0 new conversations.", output)
        self.assertIn("skipped 6 already imported SMS", output)
        self.assertIn("skipped 2 already imported MMS", output)
        self.assertIn("skipped 8 already imported calls", output)
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events"), [(17,)])
        self.assertEqual(self.files(), files)

    def test_dry_run_changes_nothing(self):
        code, output = self.run_cli("--import", SAMPLES, CALLS, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("Would import 6 SMS, 2 MMS (1 attachment) and 8 calls, 3 new conversations.", output)
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events"), [(1,)])
        self.assertFalse(os.path.exists(self.data_dir))
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_failed_import_leaves_no_trace(self):
        with mock.patch.object(si.Importer, "finish", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_cli("--import", SAMPLES, CALLS, "--yes")
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM Groups"), [(1,)])
        self.assertEqual(self.files(), [])

    def test_backup_and_restore(self):
        self.assertEqual(self.run_cli("--import", SAMPLES, CALLS, "--yes")[0], 0)
        backup, = glob.glob(os.path.join(self.backup_dir, "commhistory-*.db"))
        code, output = self.run_cli("--restore", backup, "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.query("SELECT COUNT(*) FROM Events"), [(1,)])
        self.assertEqual(self.query("PRAGMA journal_mode"), [("wal",)])
        self.assertEqual(len(glob.glob(os.path.join(self.backup_dir, "before-restore-*.db"))), 1)
        code, output = self.run_cli("--backup")
        self.assertEqual(code, 0, output)
        self.assertIn("Backup written to", output)

    def test_missing_database(self):
        os.remove(self.db)
        code, output = self.run_cli("--import", SAMPLES, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Database not found", output)

    def test_sim_selection(self):
        accounts = {RIL0, RIL1}
        conn = sqlite3.connect(self.db)
        self.assertEqual(si.choose_local_uid(conn, accounts=accounts)[0], RIL1)
        # Events of the old importer's non-existent "account0" are ignored.
        add_native_sms(conn, "+4900000000", 1790000001, local_uid=si.RING_ACCOUNT + "account0")
        self.assertEqual(si.choose_local_uid(conn, accounts=accounts)[0], RIL1)
        self.assertEqual(si.choose_local_uid(conn, sim=1)[0], RIL0)
        self.assertEqual(si.find_subscriber_identity(conn, RIL1), IMSI)
        self.assertIsNone(si.find_subscriber_identity(conn, RIL0))
        conn.close()
        empty_dir = tempfile.mkdtemp(dir=self.tmp)
        empty = sqlite3.connect(create_db(empty_dir))
        with self.assertRaises(si.UserError):
            si.choose_local_uid(empty, accounts=accounts)
        self.assertEqual(si.choose_local_uid(empty, accounts={RIL0})[0], RIL0)
        empty.close()

    def test_new_conversation_prefers_international_form(self):
        conn = si.connect(self.db)
        conn.execute("BEGIN IMMEDIATE")
        importer = si.Importer(conn, RIL1, None, self.data_dir)
        for start, address in ((1000000000, "0151 12345678"), (1000000100, "+4915112345678"),
                               (1000000200, "015112345678")):
            event = si.Event("sms", si.TYPE_SMS, si.INBOUND, start, address)
            event.free_text = "hello %d" % start
            importer.add(event)
        importer.finish()
        conn.execute("COMMIT")
        conn.close()
        self.assertEqual(importer.stats["conversations"], 1)
        self.assertEqual(self.query("SELECT remoteUids FROM Groups WHERE id > ?", (self.native_group,)),
                         [("+4915112345678",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM EventProperties WHERE eventId > 1"), [(0,)])


if __name__ == "__main__":
    unittest.main()
