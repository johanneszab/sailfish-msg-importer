-- Schema of ~/.local/share/commhistory/commhistory.db as found on Sailfish OS 5.2.0.22
-- (libcommhistory schema version 4, no data). Used by the tests.
PRAGMA encoding = "UTF-16";
PRAGMA journal_mode = WAL;
CREATE TABLE Groups ( id INTEGER PRIMARY KEY AUTOINCREMENT, localUid TEXT, remoteUids TEXT, type INTEGER, chatName TEXT, lastModified INTEGER UNSIGNED );
CREATE TABLE Events ( id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, startTime INTEGER, endTime INTEGER, direction INTEGER, isDraft INTEGER, isRead INTEGER, isMissedCall INTEGER, isEmergencyCall INTEGER, status INTEGER, bytesReceived INTEGER, localUid TEXT, remoteUid TEXT, parentId INTEGER, subject TEXT, freeText TEXT, groupId INTEGER, messageToken TEXT, lastModified INTEGER, vCardFileName TEXT, vCardLabel TEXT, isDeleted INTEGER, reportDelivery INTEGER, validityPeriod INTEGER, contentLocation TEXT, messageParts TEXT, headers TEXT, readStatus INTEGER, reportRead INTEGER, reportedReadRequested INTEGER, mmsId TEXT, isAction INTEGER, hasExtraProperties BOOL DEFAULT 0, hasMessageParts BOOL DEFAULT 0, FOREIGN KEY(groupId) REFERENCES Groups(id) ON DELETE CASCADE );
CREATE INDEX events_remoteUid ON Events (remoteUid);
CREATE INDEX events_type ON Events (type);
CREATE INDEX events_messageToken ON Events (messageToken);
CREATE INDEX events_sorting ON Events (groupId, endTime DESC, id DESC);
CREATE INDEX events_unread ON Events (isRead);
CREATE TABLE EventProperties ( eventId INTEGER, key TEXT, value BLOB, FOREIGN KEY (eventId) REFERENCES Events(id) ON DELETE CASCADE, PRIMARY KEY (eventId, key) ON CONFLICT REPLACE );
CREATE TRIGGER eventproperties_flag_insert AFTER INSERT ON EventProperties BEGIN UPDATE Events SET hasExtraProperties=1 WHERE id=NEW.eventId; END;
CREATE TRIGGER eventproperties_flag_delete AFTER DELETE ON EventProperties WHEN (SELECT COUNT(*) FROM EventProperties WHERE eventId=OLD.eventId) = 0 BEGIN UPDATE Events SET hasExtraProperties=0 WHERE id=OLD.eventId; END;
CREATE TABLE MessageParts ( id INTEGER PRIMARY KEY AUTOINCREMENT, eventId INTEGER, contentId TEXT, contentType TEXT, path TEXT, FOREIGN KEY (eventId) REFERENCES Events(id) ON DELETE SET NULL );
CREATE INDEX messageparts_eventId ON MessageParts (eventId);
CREATE TRIGGER messageparts_flag_insert AFTER INSERT ON MessageParts BEGIN UPDATE Events SET hasMessageParts=1 WHERE id=NEW.eventId; END;
CREATE TRIGGER messageparts_flag_delete AFTER DELETE ON MessageParts WHEN (SELECT COUNT(*) FROM MessageParts WHERE eventId=OLD.eventId) = 0 BEGIN UPDATE Events SET hasMessageParts=0 WHERE id=OLD.eventId; END;
PRAGMA user_version = 4;
