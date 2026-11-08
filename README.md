# sailfish-msg-importer

Imports the SMS, MMS and call history exported by the Android app
[SMS Backup & Restore](https://play.google.com/store/apps/details?id=com.riteshsahu.SMSBackupRestore)
into Sailfish OS, so it shows up in the Messages and Phone apps.

This is a fork of [javaes/sailfish-msg-importer](https://github.com/javaes/sailfish-msg-importer).
Its 2018 version no longer imports correctly on current phones, so it was
rewritten in 2026 for **Sailfish OS 5**.

## Tested with

| Component | Version |
| --- | --- |
| Sailfish OS | 5.2.0.22 (Finlayson), tested November 2026 |
| Phone | Sony Xperia 10 VI |
| Python on the phone | 3.11.17 with `python3-sqlite` |
| SMS Backup & Restore | 10.29.002 |
| Communication history database | libcommhistory schema version 4 (same as in 2018, except that `mmsId` became `TEXT`) |

On that phone, imported SMS, MMS and calls were checked in the apps. The
Messages app showed MMS pictures and emoji correctly, and merged messages from
one number written in different formats into one conversation. The Phone app
showed missed, rejected and private-number calls correctly. Restoring the
automatic backup brought back the exact previous state. The unit tests pass on
the phone and on a PC.

Please keep your Android backup files: like any tool that writes into the
phone's database directly, this script comes without guarantees.

## What gets imported

| From the backup | In Sailfish OS |
| --- | --- |
| Received and sent SMS | Messages app. Drafts and unsent or failed messages are skipped. |
| Received and sent MMS | Messages app, with text and attachments (pictures, audio, vCards…). Sailfish OS has no group conversations, so a group MMS goes to the conversation with its sender (received) or first recipient (sent), as the phone itself would file it. |
| Incoming, outgoing and missed calls | Phone app call history. Rejected and blocked calls appear as rejected, voicemail entries as missed calls, and calls from withheld numbers as "Private number". |

* Everything is imported as *read*, so old messages don't show up as unread
  (use `--keep-unread` to keep Android's flag).
* Messages from one number in different formats (`0176…` and `+49176…`) end up
  in the same conversation, and in the existing one if you already texted that
  person on Sailfish. This uses the same rule as the phone: the last 8 digits.
* Entries that are already in the database are skipped. Running the import
  twice, or importing overlapping backups, does not create duplicates.

## Requirements

* [Developer mode](https://docs.sailfishos.org/Support/Help_Articles/Enabling_Developer_Mode/)
  enabled, and a terminal on the phone (the Terminal app or SSH).
* Python's SQLite module, which is not installed by default:

      devel-su pkcon install python3-sqlite

## Usage

1. In SMS Backup & Restore on Android, back up messages and/or call logs to
   local XML files, and copy them to the phone together with `sms_import.py`
   (for example via USB into `~/Downloads`).
2. Optional but recommended: switch on flight mode during the import, so no
   new message arrives while the database is being changed.
3. Open a terminal on the phone **as the normal user** (no `devel-su`). Check what
   would be imported:

       cd ~/Downloads
       python3 sms_import.py --import sms-20260101120000.xml calls-20260101120000.xml --dry-run

4. Import:

       python3 sms_import.py --import sms-20260101120000.xml calls-20260101120000.xml

   The script backs up the database to `~/commhistory-backups/`, imports
   everything in one transaction, and restarts `commhistoryd` and the Messages
   and Phone apps so they pick up the new data.
5. Check the Messages and Phone apps.

If something looks wrong, put the backup back and reboot:

    python3 sms_import.py --restore ~/commhistory-backups/commhistory-20261108-140000.db

The restore first saves the current database as `before-restore-….db`. Anything
received after the backup was taken is gone after the restore.

### Options

| Option | Meaning |
| --- | --- |
| `--import XML [XML …]` | Import SMS/MMS and/or call log files (the type is detected per file). |
| `-n`, `--dry-run` | Show what would be imported and change nothing. |
| `--backup` | Only back up the database. |
| `--restore BACKUP_DB` | Replace the database with a backup made by this script. |
| `--sim 1\|2` | Attach the history to this SIM slot. By default the script uses the SIM of the most recent call or message on the phone. It needs this option only if the phone has no calls or messages yet. |
| `--keep-unread` | Keep Android's unread flag instead of importing everything as read. |
| `--no-mms` | Skip MMS. |
| `--allow-duplicates` | Import entries even if they are already in the database. |
| `--no-restart` | Don't restart `commhistoryd` and the apps (then reboot before using them). |
| `-y`, `--yes` | Don't ask for confirmation. |
| `--db`, `--backup-dir` | Other database or backup location (default `~/.local/share/commhistory/commhistory.db` and `~/commhistory-backups`). |

## How it works

Calls and messages live in `~/.local/share/commhistory/commhistory.db`
(libcommhistory, schema version 4, SQLite in WAL mode). The script writes rows
in the same form as `commhistoryd` and `voicecall-manager` write them on a
Sailfish OS 5 phone:

* Events are attached to the cellular Telepathy account of the SIM in use
  (`/org/freedesktop/Telepathy/Account/ring/tel/ril_0` for slot 1, `ril_1` for
  slot 2). Each event carries the SIM's `subscriberIdentity`, like native events.
* Rejected calls get the `incomingStatus` event property. MMS parts are stored as
  files in `~/.local/share/commhistory/data/<event id>/` and listed in the
  `MessageParts` table.
* `commhistoryd` keeps the list of conversations in memory, so it is restarted
  after the import. Otherwise new messages from imported contacts would open a
  second conversation.
* The XML is read incrementally, so large backups with attachments don't need
  much memory. Emoji that SMS Backup & Restore writes as invalid XML
  (`&#55357;&#56832;`) are repaired while reading.
* Backups and restores use SQLite's backup API, which is safe while other
  processes have the database open.

Don't run the script as root: files created by root would lock the apps out of
their database, so the script refuses to start.

Run the tests on a PC or on the phone with:

    python3 -m unittest discover -s tests -v

## Changes since the 2018 version

* Works on Sailfish OS 5: real SIM accounts instead of the obsolete
  `ring/tel/account0`, integer timestamps, `subscriberIdentity`, and the phone's
  conversation matching rule.
* New: call log import, MMS import, duplicate detection, dry run, SIM selection.
* Fixed: SQL built by string formatting (a `'` in an address broke the import),
  the import aborting on drafts, and restore copying a file over the live WAL
  database (which can corrupt it).
* One self-contained script (`sms_import.py`); `sail_sms.py` is gone.
