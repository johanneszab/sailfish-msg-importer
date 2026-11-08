#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# sms_import.py - import Android "SMS Backup & Restore" XML backups (SMS, MMS
# and call log) into the Sailfish OS communication history, i.e. the database
# behind the Messages and Phone apps.
#
# Based on sailfish-msg-importer by Jan van Esdonk (javaes).
# Modified in 2026: rewritten for Sailfish OS 5.x (libcommhistory schema v4):
# SIM account detection, call log and MMS import, duplicate detection,
# streaming XML parsing, repair of invalid character references, and
# backup/restore through the SQLite backup API.
#
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Import SMS, MMS and calls from "SMS Backup & Restore" into Sailfish OS.

Run it on the phone as the normal user (no devel-su needed), e.g.

    python3 sms_import.py --import sms-20260101120000.xml calls-20260101120000.xml

See README.md for the full instructions.
"""

import argparse
import base64
import binascii
import collections
import datetime
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from urllib.request import pathname2url

try:
    import sqlite3
except ImportError:  # Sailfish OS ships the module in a separate package
    sys.exit("Python's sqlite3 module is missing. Install it on the phone with:\n"
             "    devel-su pkcon install python3-sqlite")

__version__ = "2.0"

DEFAULT_DB = os.path.expanduser("~/.local/share/commhistory/commhistory.db")
DEFAULT_BACKUP_DIR = os.path.expanduser("~/commhistory-backups")
TELEPATHY_ACCOUNTS = os.path.expanduser("~/.local/share/telepathy/mission-control/accounts.cfg")
ACCOUNT_PREFIX = "/org/freedesktop/Telepathy/Account/"
RING_ACCOUNT = ACCOUNT_PREFIX + "ring/tel/"  # cellular accounts: ring/tel/ril_0, ril_1
SCHEMA_VERSION = 4
# Services that cache the database contents and must be restarted after a change.
SERVICES = ("commhistoryd.service", "jolla-messages.service", "voicecall-ui-prestart.service")

# libcommhistory enums (src/event.h)
TYPE_SMS, TYPE_CALL, TYPE_MMS = 2, 3, 6
INBOUND, OUTBOUND = 1, 2
STATUS_RECEIVED, STATUS_UNKNOWN, STATUS_SENT = -1, 0, 2

# Android: Telephony.TextBasedSmsColumns.MESSAGE_TYPE_* and BaseMmsColumns.MESSAGE_BOX_*
# (1 = received, 2 = sent; drafts, outbox, failed and queued messages are skipped)
BOX_INBOX, BOX_SENT = 1, 2
MMS_NOTIFICATION_IND = 130  # m_type of an MMS that was never downloaded
ADDR_FROM, ADDR_TO, ADDR_CC, ADDR_BCC = 137, 151, 130, 129  # MMS PDU address types
# Android: CallLog.Calls.*_TYPE -> (direction, missed, "incomingStatus" event property)
CALL_TYPES = {
    1: (INBOUND, False, None),        # incoming
    2: (OUTBOUND, False, None),       # outgoing
    3: (INBOUND, True, None),         # missed
    4: (INBOUND, True, None),         # voicemail
    5: (INBOUND, True, "rejected"),   # rejected
    6: (INBOUND, True, "rejected"),   # blocked
    7: (INBOUND, False, None),        # answered on another device
}
CHARSETS = {3: "ascii", 4: "latin-1", 106: "utf-8", 1013: "utf-16-be", 1014: "utf-16-le", 1015: "utf-16"}
EXTENSIONS = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/heic": ".heic", "video/mp4": ".mp4",
    "video/3gpp": ".3gp", "audio/amr": ".amr", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "text/plain": ".txt", "text/x-vcard": ".vcf", "text/vcard": ".vcf",
    "text/x-vcalendar": ".vcs", "application/smil": ".smil",
}


class UserError(Exception):
    """An error that is reported to the user without a traceback."""


# ---------------------------------------------------------------------------
# Phone number matching, ported from qtcontacts-sqlite / libcommhistory so that
# imported messages land in the same conversation the phone itself would use.
# ---------------------------------------------------------------------------

_SEPARATORS = " .-()[]"
_DTMF = "pPwWxX,;#*"


def minimize_phone_number(number, max_chars=8):
    """Port of QtContactsSqliteExtensions::minimizePhoneNumber()."""
    if number.startswith("sips:") or number.startswith(":"):
        colon = number.find(":")
        at = number.find("@", colon + 1)
        if at != -1:
            number = number[colon + 1:at]
    subset = []
    initial = None
    numeric = False
    first_dtmf = -1
    for ch in number:
        if ch.isdecimal():
            digit = str(unicodedata.decimal(ch))
            subset.append(digit)
            numeric = True
            if initial is None:
                initial = digit
        elif ch == "+":
            if initial is None:
                subset.append(ch)
                initial = ch
            elif first_dtmf != -1:
                subset.append(ch)
        elif ch in _SEPARATORS:
            continue
        elif ch in _DTMF:
            if ch.isalpha() and not numeric:
                continue
            if first_dtmf == -1:
                first_dtmf = len(subset)
            if ch in "xX,":
                subset.append("p")
            elif ch == ";":
                subset.append("w")
            else:
                subset.append(ch)
    if max_chars != -1:
        count = 0
        index = len(subset) - 1 if first_dtmf == -1 else first_dtmf - 1
        while index > 0:
            if subset[index].isdigit() or subset[index] == "+":
                count += 1
                if count == max_chars:
                    subset = subset[index:]
                    break
            index -= 1
    return "".join(subset).strip()


def minimize_remote_uid(remote_uid):
    """The key libcommhistory uses to decide whether two SMS addresses match.

    Phone numbers compare by their last 8 digits ("0176 123..." and
    "+49176123..." are the same conversation); anything else, such as an
    alphanumeric sender name, compares case-insensitively.
    """
    minimized = minimize_phone_number(remote_uid).lower()
    return minimized if minimized else remote_uid.lower()


# ---------------------------------------------------------------------------
# Reading SMS Backup & Restore files
# ---------------------------------------------------------------------------

_CHAR_REF = re.compile(rb"&#([xX][0-9a-fA-F]{1,8}|[0-9]{1,10});")
_RAW_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _valid_xml_char(cp):
    return (cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF)


def _ref_value(match):
    text = match.group(1)
    return int(text[1:], 16) if text[:1] in (b"x", b"X") else int(text)


def repair_xml_line(line):
    """Make one line of a backup file acceptable to an XML 1.0 parser.

    SMS Backup & Restore writes emoji as two UTF-16 surrogate references
    ("&#55357;&#56832;") and sometimes control characters ("&#1;"). XML forbids
    both, so standard parsers reject the whole file. Surrogate pairs are joined
    into one valid reference, a lone surrogate (half an emoji) becomes U+FFFD
    and other forbidden characters (invisible control codes) are dropped.
    """
    if b"&#" in line:
        matches = list(_CHAR_REF.finditer(line))
        out = []
        last = 0
        i = 0
        while i < len(matches):
            match = matches[i]
            cp = _ref_value(match)
            out.append(line[last:match.start()])
            last = match.end()
            i += 1
            if 0xD800 <= cp <= 0xDBFF and i < len(matches) and matches[i].start() == match.end():
                low = _ref_value(matches[i])
                if 0xDC00 <= low <= 0xDFFF:
                    out.append(b"&#%d;" % (0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)))
                    last = matches[i].end()
                    i += 1
                    continue
            if _valid_xml_char(cp):
                out.append(match.group(0))
            elif 0xD800 <= cp <= 0xDFFF:
                out.append(b"&#65533;")
        out.append(line[last:])
        line = b"".join(out)
    return _RAW_CONTROL.sub(b"", line)


class _RepairedXmlStream(object):
    """Binary file wrapper that repairs every line while the parser reads it."""

    def __init__(self, path):
        self._file = open(path, "rb")

    def read(self, size=-1):
        line = self._file.readline()
        return repair_xml_line(line) if line else b""

    def close(self):
        self._file.close()


def iter_backup_records(path):
    """Yield (tag, attributes, parts, addresses) for each sms, mms and call.

    The file is parsed incrementally, so even backups with hundreds of MB of
    MMS attachments only need memory for one message at a time.
    """
    stream = _RepairedXmlStream(path)
    try:
        root = None
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                continue
            if elem.tag in ("sms", "call"):
                yield elem.tag, dict(elem.attrib), [], []
            elif elem.tag == "mms":
                parts = [dict(part.attrib) for part in elem.iter("part")]
                addrs = [dict(addr.attrib) for addr in elem.iter("addr")]
                yield "mms", dict(elem.attrib), parts, addrs
            else:
                continue
            root.clear()
    except ET.ParseError as error:
        raise UserError("%s is not a valid backup file: %s" % (path, error))
    finally:
        stream.close()


def _attr(attrs, name, default=None):
    value = attrs.get(name)
    return default if value is None or value == "null" else value


def _int_attr(attrs, name, default=None):
    try:
        return int(_attr(attrs, name))
    except (TypeError, ValueError):
        return default


def _epoch_seconds(value):
    """Backup timestamps are Java milliseconds; tolerate plain seconds too."""
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    if stamp > 10 ** 11:
        stamp //= 1000
    return stamp if stamp > 0 else None


class Event(object):
    """One row for the Events table, plus its event properties and MMS parts."""

    def __init__(self, kind, event_type, direction, start, remote_uid):
        self.kind = kind  # "sms", "mms" or "call"
        self.type = event_type
        self.direction = direction
        self.start = start
        self.end = start
        self.remote_uid = remote_uid
        self.free_text = None
        self.subject = None
        self.is_read = True
        self.is_missed = False
        self.status = STATUS_UNKNOWN
        self.headers = None
        self.properties = {}
        self.parts = []  # (content id, content type, file name, payload bytes)

    def duplicate_key(self):
        return duplicate_key(self.type, self.direction, self.start, self.end,
                             self.remote_uid, self.free_text, self.is_missed)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def duplicate_key(event_type, direction, start, end, remote_uid, free_text, is_missed):
    """Identity of an event, used to skip what is already in the database."""
    start = _as_int(start)
    uid = minimize_remote_uid(remote_uid or "")
    if event_type == TYPE_CALL:
        return (event_type, direction, start, uid, _as_int(end, start) - start, bool(is_missed))
    return (event_type, direction, start, uid, free_text or "")


def sms_to_event(attrs, keep_unread=False):
    """Convert an <sms> element. Returns (event, None) or (None, skip reason)."""
    box = _int_attr(attrs, "type")
    if box not in (BOX_INBOX, BOX_SENT):
        return None, "drafts/unsent SMS"
    start = _epoch_seconds(_attr(attrs, "date"))
    address = (_attr(attrs, "address") or "").strip()
    if start is None or not address:
        return None, "SMS without date or address"
    event = Event("sms", TYPE_SMS, INBOUND if box == BOX_INBOX else OUTBOUND, start, address)
    event.free_text = _attr(attrs, "body", "")
    if event.direction == OUTBOUND:
        event.status = STATUS_SENT
    elif keep_unread:
        event.is_read = _attr(attrs, "read") != "0"
    return event, None


def call_to_event(attrs):
    """Convert a <call> element. Returns (event, None) or (None, skip reason)."""
    mapping = CALL_TYPES.get(_int_attr(attrs, "type"))
    if mapping is None:
        return None, "calls of unknown type"
    start = _epoch_seconds(_attr(attrs, "date"))
    if start is None:
        return None, "calls without date"
    direction, missed, incoming_status = mapping
    number = (_attr(attrs, "number") or "").strip()
    if _int_attr(attrs, "presentation", 1) != 1 or number in ("-1", "-2", "-3"):
        number = ""  # withheld/unknown/payphone: the Phone app shows "Private number"
    elif number:
        number += _attr(attrs, "post_dial_digits", "") or ""
    event = Event("call", TYPE_CALL, direction, start, number)
    if not missed:
        event.end = start + max(_int_attr(attrs, "duration", 0) or 0, 0)
    event.is_missed = missed
    if incoming_status:
        event.properties["incomingStatus"] = incoming_status
    return event, None


def _part_file_name(part, content_type, index):
    base = ""
    for key in ("cl", "name", "fn", "cid"):
        base = os.path.basename((_attr(part, key) or "").strip("<> "))
        if base:
            break
    name = re.sub(r"[^-.0-9a-zA-Z]", "_", base)[-100:].lstrip(".")
    if not name.strip("_"):
        name = "part%d" % index
    extension = EXTENSIONS.get(content_type.split(";")[0].strip().lower())
    if extension and not os.path.splitext(name)[1]:
        name += extension
    return name


def mms_to_event(attrs, parts, addrs, keep_unread=False):
    """Convert an <mms> element the way commhistoryd stores a received MMS.

    Sailfish OS has no group MMS conversations: like the phone itself, an
    incoming MMS goes to the conversation with its sender and an outgoing one
    to the conversation with its first recipient; the full recipient lists are
    kept in the x-mms-to/x-mms-cc headers.
    """
    box = _int_attr(attrs, "msg_box")
    if box not in (BOX_INBOX, BOX_SENT):
        return None, "drafts/unsent MMS"
    if _int_attr(attrs, "m_type") == MMS_NOTIFICATION_IND:
        return None, "MMS that were never downloaded"
    start = _epoch_seconds(_attr(attrs, "date"))
    if start is None:
        return None, "MMS without date"

    by_type = collections.defaultdict(list)
    for addr in addrs:
        address = (_attr(addr, "address") or "").strip()
        if address and address != "insert-address-token":
            by_type[_int_attr(addr, "type")].append(address)
    fallback = [a.strip() for a in (_attr(attrs, "address") or "").split("~") if a.strip()]
    if box == BOX_INBOX:
        candidates = by_type[ADDR_FROM] or fallback[:1]
    else:
        candidates = by_type[ADDR_TO] or by_type[ADDR_CC] or by_type[ADDR_BCC] or fallback
    if not candidates:
        return None, "MMS without address"

    event = Event("mms", TYPE_MMS, INBOUND if box == BOX_INBOX else OUTBOUND, start, candidates[0])
    event.status = STATUS_RECEIVED if box == BOX_INBOX else STATUS_SENT
    event.subject = _attr(attrs, "sub") or None
    if box == BOX_INBOX and keep_unread:
        event.is_read = _attr(attrs, "read") != "0"
    headers = []
    if by_type[ADDR_TO]:
        headers.append("x-mms-to\x1d" + "\x1e".join(by_type[ADDR_TO]))
    if by_type[ADDR_CC]:
        headers.append("x-mms-cc\x1d" + "\x1e".join(by_type[ADDR_CC]))
    event.headers = "\x1c".join(headers) or None

    texts = []
    for index, part in enumerate(parts):
        content_type = (_attr(part, "ct") or "application/octet-stream").strip()
        mime = content_type.split(";")[0].strip().lower()
        data = _attr(part, "data")
        text = _attr(part, "text")
        if data:
            try:
                payload = base64.b64decode(data)
            except (ValueError, binascii.Error):
                continue
            if mime.startswith("text/"):
                charset = CHARSETS.get(_int_attr(part, "chset"), "utf-8")
                text = payload.decode(charset, "replace")
        elif text is None:
            continue
        if mime.startswith("text/"):
            payload = text.encode("utf-8")
            content_type = mime + ";charset=utf-8"  # read by MessagePart::plainTextContent()
            if mime == "text/plain" and text.strip():
                texts.append(text.strip())
        elif not data:
            payload = text.encode("utf-8")  # e.g. the SMIL layout
        name = _part_file_name(part, content_type, index)
        content_id = (_attr(part, "cid") or "").strip().strip("<>") or name
        event.parts.append((content_id, content_type, name, payload))
    event.free_text = "\n".join(texts)
    return event, None


def read_backup(path, keep_unread=False, include_mms=True, skipped=None):
    """Read one backup file and return the list of events it contains."""
    events = []
    skipped = skipped if skipped is not None else collections.Counter()
    for tag, attrs, parts, addrs in iter_backup_records(path):
        if tag == "sms":
            event, reason = sms_to_event(attrs, keep_unread)
        elif tag == "call":
            event, reason = call_to_event(attrs)
        elif include_mms:
            event, reason = mms_to_event(attrs, parts, addrs, keep_unread)
        else:
            event, reason = None, "MMS (--no-mms)"
        if event is None:
            skipped[reason] += 1
        else:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Writing to commhistory.db
# ---------------------------------------------------------------------------

INSERT_EVENT = """
INSERT INTO Events (type, startTime, endTime, direction, isDraft, isRead, isMissedCall,
    isEmergencyCall, status, bytesReceived, localUid, remoteUid, parentId, subject, freeText,
    groupId, messageToken, lastModified, vCardFileName, vCardLabel, isDeleted, reportDelivery,
    validityPeriod, contentLocation, messageParts, headers, readStatus, reportRead,
    reportedReadRequested, mmsId, isAction, hasExtraProperties, hasMessageParts)
VALUES (?, ?, ?, ?, 0, ?, ?, 0, ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 0, NULL,
    NULL, ?, 0, 0, 0, NULL, 0, 0, 0)
"""


class Importer(object):
    """Adds events to an open commhistory database (inside a transaction)."""

    def __init__(self, conn, local_uid, subscriber_identity=None, data_dir=None,
                 write_files=True, skip_duplicates=True):
        self.conn = conn
        self.local_uid = local_uid
        self.subscriber_identity = subscriber_identity
        self.data_dir = data_dir
        self.write_files = write_files
        self.skip_duplicates = skip_duplicates
        self.token_prefix = "/%s/message_" % local_uid.rstrip("/").rsplit("/", 1)[-1]
        self.stats = collections.Counter()
        self.groups = {}
        self.new_groups = {}  # group id -> Counter of the addresses used for it
        self.seen = set()
        self.written_files = []
        self.created_dirs = []
        for group_id, group_local_uid, remote_uids in conn.execute(
                "SELECT id, localUid, remoteUids FROM Groups ORDER BY id"):
            key = self._group_key(group_local_uid or "", (remote_uids or "").split("\n"))
            self.groups.setdefault(key, group_id)
        if skip_duplicates:
            for row in conn.execute("SELECT type, direction, startTime, endTime, remoteUid, freeText,"
                                    " isMissedCall FROM Events WHERE type IN (2, 3, 6)"):
                self.seen.add(duplicate_key(*row))

    @staticmethod
    def _group_key(local_uid, remote_uids):
        # Same rule as GroupManager::findGroup(): identical account, matching recipients.
        if local_uid.startswith(RING_ACCOUNT):
            uids = frozenset(minimize_remote_uid(uid) for uid in remote_uids if uid)
        else:
            uids = frozenset(uid.lower() for uid in remote_uids if uid)
        return local_uid, uids

    def _group_for(self, remote_uid):
        key = self._group_key(self.local_uid, [remote_uid])
        group_id = self.groups.get(key)
        if group_id is None:
            group_id = self.conn.execute(
                "INSERT INTO Groups (localUid, remoteUids, type, chatName, lastModified)"
                " VALUES (?, ?, 0, NULL, 0)", (self.local_uid, remote_uid)).lastrowid
            self.groups[key] = group_id
            self.new_groups[group_id] = collections.Counter()
            self.stats["conversations"] += 1
        if group_id in self.new_groups:
            self.new_groups[group_id][remote_uid] += 1
        return group_id

    def add(self, event):
        """Insert one event; returns False if it is already in the database."""
        key = event.duplicate_key()
        if self.skip_duplicates and key in self.seen:
            self.stats["duplicate " + event.kind] += 1
            return False
        self.seen.add(key)

        group_id = None if event.type == TYPE_CALL else self._group_for(event.remote_uid)
        token = None
        if event.type == TYPE_SMS:
            if event.direction == INBOUND:
                token = str(uuid.uuid4())
            else:
                token = self.token_prefix + os.urandom(20).hex().upper()
        # Same pattern as events written by the phone itself.
        last_modified = 0 if event.type != TYPE_CALL and event.direction == INBOUND else event.start
        event_id = self.conn.execute(INSERT_EVENT, (
            event.type, event.start, event.end, event.direction, int(event.is_read),
            int(event.is_missed), event.status, self.local_uid, event.remote_uid, event.subject,
            event.free_text, group_id, token, last_modified, event.headers)).lastrowid

        properties = dict(event.properties)
        if self.subscriber_identity:
            properties["subscriberIdentity"] = self.subscriber_identity
        for name, value in sorted(properties.items()):
            self.conn.execute("INSERT INTO EventProperties (eventId, key, value) VALUES (?, ?, ?)",
                              (event_id, name, value))
        if event.parts:
            self._add_parts(event_id, event.parts)
        self.stats[event.kind] += 1
        return True

    def _add_parts(self, event_id, parts):
        directory = os.path.join(self.data_dir, str(event_id))
        used = set()
        if self.write_files:
            if os.path.isdir(directory):
                used.update(os.listdir(directory))
            else:
                os.makedirs(directory)
                self.created_dirs.append(directory)
        for content_id, content_type, name, payload in parts:
            stem, extension = os.path.splitext(name)
            counter = 1
            while name in used:
                name = "%s-%d%s" % (stem, counter, extension)
                counter += 1
            used.add(name)
            path = os.path.join(directory, name)
            if self.write_files:
                with open(path, "xb") as part_file:
                    self.written_files.append(path)
                    part_file.write(payload)
            self.conn.execute("INSERT INTO MessageParts (eventId, contentId, contentType, path)"
                              " VALUES (?, ?, ?, ?)", (event_id, content_id, content_type, path))
            if not content_type.startswith(("application/smil", "text/plain")):
                self.stats["attachments"] += 1

    def finish(self):
        """Prefer the international form (+49...) as the name of new conversations."""
        for group_id, addresses in self.new_groups.items():
            ranked = sorted(addresses.items(), key=lambda item: (not item[0].startswith("+"), -item[1]))
            if ranked:
                self.conn.execute("UPDATE Groups SET remoteUids = ? WHERE id = ?", (ranked[0][0], group_id))

    def discard_files(self):
        """Remove the MMS files written so far (after a rollback)."""
        for path in reversed(self.written_files):
            try:
                os.remove(path)
            except OSError:
                pass
        for directory in reversed(self.created_dirs):
            try:
                os.rmdir(directory)
            except OSError:
                pass


def connect(db_path, timeout=60):
    if not os.path.isfile(db_path):
        raise UserError("Database not found: %s\n(Run this on the phone, or pass --db.)" % db_path)
    conn = sqlite3.connect("file:%s?mode=rw" % pathname2url(os.path.abspath(db_path)),
                           uri=True, timeout=timeout)
    conn.isolation_level = None  # explicit BEGIN/COMMIT below
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def check_schema(conn):
    required = {
        "Events": {"id", "type", "startTime", "endTime", "direction", "isRead", "isMissedCall",
                   "status", "localUid", "remoteUid", "freeText", "groupId", "messageToken",
                   "lastModified", "headers", "hasExtraProperties", "hasMessageParts"},
        "Groups": {"id", "localUid", "remoteUids", "type", "chatName", "lastModified"},
        "EventProperties": {"eventId", "key", "value"},
        "MessageParts": {"eventId", "contentId", "contentType", "path"},
    }
    for table, columns in required.items():
        found = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if not columns <= found:
            raise UserError("Unexpected commhistory database layout: table %s lacks %s"
                            % (table, ", ".join(sorted(columns - found)) or "everything"))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        print("Warning: database schema version is %d, this script was written for version %d."
              % (version, SCHEMA_VERSION))


def telepathy_ring_accounts(path=TELEPATHY_ACCOUNTS):
    """Cellular accounts known to the phone, e.g. {".../ring/tel/ril_0", ...}."""
    accounts = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as config:
            for line in config:
                match = re.match(r"\s*\[(ring/tel/[^\]]+)\]", line)
                if match:
                    accounts.add(ACCOUNT_PREFIX + match.group(1))
    except OSError:
        pass
    return accounts


def choose_local_uid(conn, sim=None, local_uid=None, accounts=None):
    """Pick the SIM account the imported history is attached to."""
    if local_uid:
        return local_uid, "given with --local-uid"
    if sim:
        return RING_ACCOUNT + "ril_%d" % (sim - 1), "given with --sim"
    accounts = telepathy_ring_accounts() if accounts is None else accounts
    for table in ("Events", "Groups"):
        for (uid,) in conn.execute("SELECT localUid FROM %s WHERE localUid LIKE ? "
                                   "ORDER BY id DESC" % table, (RING_ACCOUNT + "%",)):
            if not accounts or uid in accounts:
                return uid, "used by the most recent call or message on this phone"
    if len(accounts) == 1:
        return next(iter(accounts)), "the only cellular account on this phone"
    raise UserError("Cannot tell which SIM to use (no calls or messages on the phone yet).\n"
                    "Pass --sim 1 or --sim 2.")


def find_subscriber_identity(conn, local_uid):
    row = conn.execute("SELECT p.value FROM EventProperties p JOIN Events e ON e.id = p.eventId "
                       "WHERE p.key = 'subscriberIdentity' AND e.localUid = ? "
                       "ORDER BY e.id DESC LIMIT 1", (local_uid,)).fetchone()
    return str(row[0]) if row and row[0] else None


def describe_account(local_uid):
    match = re.search(r"/ril_(\d+)$", local_uid)
    return "%s (SIM %d)" % (local_uid, int(match.group(1)) + 1) if match else local_uid


def backup_database(conn, backup_dir, label="commhistory"):
    """Write a consistent single-file copy of the database and return its path."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, "%s-%s.db" % (label, stamp))
    counter = 1
    while os.path.exists(path):
        path = os.path.join(backup_dir, "%s-%s-%d.db" % (label, stamp, counter))
        counter += 1
    target = sqlite3.connect(path)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # self-contained, no -wal file
    finally:
        target.close()
    return path


def on_sailfish():
    return os.path.exists("/etc/sailfish-release")


def systemctl(*args):
    try:
        result = subprocess.run(("systemctl", "--user") + args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return result.returncode == 0, result.stdout.strip()


def start_services(action):
    """Run "systemctl --user ACTION" for each service; returns the failures.

    One at a time and with retries: the Messages and Phone apps are launched
    through the same pre-started booster process, and starting both at once
    can fail while the booster is still respawning.
    """
    failed = []
    for service in SERVICES:
        for attempt in range(4):
            if attempt:
                time.sleep(2)
            ok, output = systemctl(action, service)
            if ok:
                break
        else:
            failed.append("%s: %s" % (service, output.splitlines()[0] if output else "failed"))
    return failed


def restart_services():
    """Make the daemon and the apps reload the database."""
    failed = start_services("restart")
    if failed:
        print("Could not restart %s. Please reboot the phone now." % "; ".join(failed))
    else:
        print("Restarted the communication history daemon and the Messages/Phone apps.")


def confirm(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise UserError("Not asking for confirmation without a terminal; pass --yes.")
    return input(question + " [y/N] ").strip().lower() in ("y", "yes")


def _plural(count, word):
    return "%d %s" % (count, word if count == 1 else word + "s")


def run_import(args):
    events = []
    skipped = collections.Counter()
    for path in args.import_files:
        if not os.path.isfile(path):
            raise UserError("File not found: %s" % path)
        found = read_backup(path, args.keep_unread, not args.no_mms, skipped)
        kinds = collections.Counter(event.kind for event in found)
        print("Read %s: %d SMS, %d MMS, %s" % (path, kinds["sms"], kinds["mms"],
                                               _plural(kinds["call"], "call")))
        events.extend(found)
    events.sort(key=lambda event: (event.start, event.end))

    conn = connect(args.db)
    try:
        check_schema(conn)
        local_uid, reason = choose_local_uid(conn, args.sim, args.local_uid)
        subscriber_identity = find_subscriber_identity(conn, local_uid)
        print("Account: %s, %s" % (describe_account(local_uid), reason))
        known = telepathy_ring_accounts()
        if known and local_uid not in known:
            print("Warning: this account does not exist on the phone (known: %s)"
                  % ", ".join(sorted(known)))
        if not subscriber_identity:
            print("Note: no SIM identity known for this account yet; events are imported without it.")
        if not events:
            print("Nothing to import.")
            return 0

        if args.dry_run:
            target = sqlite3.connect(":memory:")
            conn.backup(target)
            conn.close()
            conn = target
            conn.isolation_level = None
            conn.execute("PRAGMA foreign_keys = ON")
        else:
            if on_sailfish():
                print("Tip: switch on flight mode during the import so no message arrives meanwhile.")
            if not confirm("Import %d events into %s?" % (len(events), args.db), args.yes):
                print("Aborted, nothing changed.")
                return 1
            print("Backup written to %s" % backup_database(conn, args.backup_dir))

        data_dir = os.path.join(os.path.dirname(os.path.abspath(args.db)), "data")
        conn.execute("BEGIN IMMEDIATE")
        importer = None
        try:
            # Read groups and existing events only now, under the write lock.
            importer = Importer(conn, local_uid, subscriber_identity, data_dir,
                                write_files=not args.dry_run,
                                skip_duplicates=not args.allow_duplicates)
            for event in events:
                importer.add(event)
            importer.finish()
            conn.execute("ROLLBACK" if args.dry_run else "COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            if importer:
                importer.discard_files()
            print("Import failed, the database was not changed.", file=sys.stderr)
            raise
    finally:
        conn.close()

    stats = importer.stats
    print("%s %d SMS, %d MMS (%s) and %s, %s." % (
        "Would import" if args.dry_run else "Imported", stats["sms"], stats["mms"],
        _plural(stats["attachments"], "attachment"), _plural(stats["call"], "call"),
        _plural(stats["conversations"], "new conversation")))
    for kind in ("sms", "mms", "call"):
        skipped["already imported " + ("calls" if kind == "call" else kind.upper())] += stats["duplicate " + kind]
    for reason, count in sorted(skipped.items()):
        if count:
            print("  skipped %d %s" % (count, reason))
    if not args.dry_run and not args.no_restart and on_sailfish():
        restart_services()
    return 0


def run_backup(args):
    conn = connect(args.db)
    print("Backup written to %s" % backup_database(conn, args.backup_dir))
    conn.close()
    return 0


def run_restore(args):
    backup = args.restore
    if not os.path.isfile(backup):
        raise UserError("Backup not found: %s" % backup)
    source = sqlite3.connect("file:%s?mode=ro" % pathname2url(os.path.abspath(backup)), uri=True)
    try:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UserError("%s is damaged, not restoring it." % backup)
        check_schema(source)
        count = source.execute("SELECT COUNT(*) FROM Events").fetchone()[0]
        if not confirm("Replace %s with %s (%d events)? Everything that happened since that backup "
                       "will be lost." % (args.db, backup, count), args.yes):
            print("Aborted, nothing changed.")
            return 1
        conn = connect(args.db)
        print("Current database saved to %s" % backup_database(conn, args.backup_dir, "before-restore"))
        stop = on_sailfish() and not args.no_restart
        if stop:
            systemctl("stop", *SERVICES)
        try:
            source.backup(conn)  # page-by-page copy, safe with the WAL journal
        finally:
            conn.close()
            if stop:
                for failure in start_services("start"):
                    print("Could not start %s" % failure)
    finally:
        source.close()
    print("Restored %s. Reboot the phone to make sure every app sees the restored data." % backup)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Import SMS, MMS and call history exported by the Android app "
                    "\"SMS Backup & Restore\" into Sailfish OS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 sms_import.py --import sms-2026.xml calls-2026.xml --dry-run\n"
               "  python3 sms_import.py --import sms-2026.xml calls-2026.xml\n"
               "  python3 sms_import.py --backup\n"
               "  python3 sms_import.py --restore ~/commhistory-backups/commhistory-20261108-140000.db")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--import", dest="import_files", nargs="+", metavar="XML",
                      help="SMS/MMS and/or call log backup files to import")
    mode.add_argument("--backup", action="store_true", help="only back up the database")
    mode.add_argument("--restore", metavar="BACKUP_DB",
                      help="replace the database with a backup made by this script")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would be imported without changing anything")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--sim", type=int, choices=(1, 2),
                        help="attach the history to this SIM slot (default: the SIM in use)")
    parser.add_argument("--local-uid", help=argparse.SUPPRESS)
    parser.add_argument("--keep-unread", action="store_true",
                        help="keep Android's unread flag (default: import everything as read)")
    parser.add_argument("--no-mms", action="store_true", help="skip MMS messages")
    parser.add_argument("--allow-duplicates", action="store_true",
                        help="import events even if they are already in the database")
    parser.add_argument("--no-restart", action="store_true",
                        help="do not restart commhistoryd and the Messages/Phone apps")
    parser.add_argument("--db", default=DEFAULT_DB, help="database (default: %(default)s)")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                        help="where backups are written (default: %(default)s)")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Do not run this as root (devel-su): files created by root would lock the "
              "Messages and Phone apps out of their database. Run it as the normal user.",
              file=sys.stderr)
        return 2
    try:
        if args.import_files:
            return run_import(args)
        if args.backup:
            return run_backup(args)
        return run_restore(args)
    except UserError as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print("Database error: %s" % error, file=sys.stderr)
        return 1
    except OSError as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
