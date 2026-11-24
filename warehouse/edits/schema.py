"""SQLite schema and archive helpers for rewritable edit extracts."""

from dataclasses import dataclass
from sqlite3 import Connection

from ..ledger.integer_storage import decode_integer, encode_integer


@dataclass(frozen=True, slots=True)
class EditExtractHeader:
    extract_id: str
    day: str
    revision: int


@dataclass(frozen=True, slots=True)
class FrozenPosition:
    position: int
    occurrence_id: str
    label: str
    value: int


@dataclass(frozen=True, slots=True)
class StoredRequest:
    request_id: str
    extract_id: str
    revision: int
    fingerprint: str


def create_edit_extract_schema(db: Connection) -> None:
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS edit_extracts (
            seq INTEGER PRIMARY KEY,
            extract_id TEXT NOT NULL UNIQUE,
            revision INTEGER NOT NULL REFERENCES revisions(revision),
            day TEXT NOT NULL
        )
        '''
    )
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS edit_extract_lines (
            extract_id TEXT NOT NULL REFERENCES edit_extracts(extract_id),
            position INTEGER NOT NULL CHECK (position >= 0),
            occurrence_id TEXT NOT NULL,
            label TEXT NOT NULL,
            value_hex TEXT NOT NULL,
            PRIMARY KEY (extract_id, position)
        )
        '''
    )
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS edit_requests (
            request_id TEXT PRIMARY KEY NOT NULL,
            extract_id TEXT NOT NULL REFERENCES edit_extracts(extract_id),
            revision INTEGER NOT NULL REFERENCES revisions(revision),
            fingerprint TEXT NOT NULL,
            UNIQUE (extract_id, request_id)
        )
        '''
    )
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS edit_request_group_changes (
            request_id TEXT NOT NULL REFERENCES edit_requests(request_id),
            group_id TEXT NOT NULL,
            old_offset_hex TEXT NOT NULL,
            new_offset_hex TEXT NOT NULL,
            PRIMARY KEY (request_id, group_id)
        )
        '''
    )
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS edit_request_override_changes (
            request_id TEXT NOT NULL REFERENCES edit_requests(request_id),
            occurrence_id TEXT NOT NULL,
            old_offset_hex TEXT,
            new_offset_hex TEXT,
            PRIMARY KEY (request_id, occurrence_id)
        )
        '''
    )


def save_edit_extract(
    db: Connection, day: str, revision: int, positions: tuple[FrozenPosition, ...]
) -> str:
    number = db.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM edit_extracts').fetchone()[0]
    extract_id = f'E{number}'
    db.execute(
        '''
        INSERT INTO edit_extracts (seq, extract_id, revision, day)
        VALUES (?, ?, ?, ?)
        ''',
        (number, extract_id, revision, day),
    )
    db.executemany(
        '''
        INSERT INTO edit_extract_lines (extract_id, position, occurrence_id, label, value_hex)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (
            (
                extract_id,
                position.position,
                position.occurrence_id,
                position.label,
                encode_integer(position.value),
            )
            for position in positions
        ),
    )
    return extract_id


def load_edit_extract_header(db: Connection, extract_id: str) -> EditExtractHeader:
    row = db.execute(
        'SELECT extract_id, day, revision FROM edit_extracts WHERE extract_id = ?',
        (extract_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f'unknown edit extract: {extract_id!r}')
    return EditExtractHeader(*row)


def load_edit_extract_lines(db: Connection, extract_id: str) -> tuple[FrozenPosition, ...]:
    rows = db.execute(
        '''
        SELECT position, occurrence_id, label, value_hex
        FROM edit_extract_lines WHERE extract_id = ? ORDER BY position
        ''',
        (extract_id,),
    )
    return tuple(
        FrozenPosition(position, occurrence_id, label, decode_integer(value_hex))
        for position, occurrence_id, label, value_hex in rows
    )


def make_fingerprint(targets_by_position: dict[int, int]) -> str:
    return '\n'.join(
        f'{position:x}={format(targets_by_position[position], "x")}'
        for position in sorted(targets_by_position)
    )


def save_request_receipt(
    db: Connection,
    request_id: str,
    extract_id: str,
    revision: int,
    fingerprint: str,
    group_receipt: dict[str, list[int]],
    override_receipt: dict[str, list[int | None]],
) -> None:
    db.execute(
        '''
        INSERT INTO edit_requests (request_id, extract_id, revision, fingerprint)
        VALUES (?, ?, ?, ?)
        ''',
        (request_id, extract_id, revision, fingerprint),
    )
    db.executemany(
        '''
        INSERT INTO edit_request_group_changes (request_id, group_id, old_offset_hex, new_offset_hex)
        VALUES (?, ?, ?, ?)
        ''',
        (
            (
                request_id,
                group_id,
                encode_integer(group_receipt[group_id][0]),
                encode_integer(group_receipt[group_id][1]),
            )
            for group_id in sorted(group_receipt)
        ),
    )
    db.executemany(
        '''
        INSERT INTO edit_request_override_changes (
            request_id, occurrence_id, old_offset_hex, new_offset_hex
        ) VALUES (?, ?, ?, ?)
        ''',
        (
            (
                request_id,
                occurrence_id,
                None
                if override_receipt[occurrence_id][0] is None
                else encode_integer(override_receipt[occurrence_id][0]),
                None
                if override_receipt[occurrence_id][1] is None
                else encode_integer(override_receipt[occurrence_id][1]),
            )
            for occurrence_id in sorted(override_receipt)
        ),
    )


def load_stored_request(db: Connection, request_id: str) -> StoredRequest | None:
    row = db.execute(
        '''
        SELECT request_id, extract_id, revision, fingerprint
        FROM edit_requests WHERE request_id = ?
        ''',
        (request_id,),
    ).fetchone()
    return StoredRequest(*row) if row is not None else None


def load_request_receipt(db: Connection, request_id: str) -> dict:
    stored = load_stored_request(db, request_id)
    group_rows = db.execute(
        '''
        SELECT group_id, old_offset_hex, new_offset_hex
        FROM edit_request_group_changes WHERE request_id = ?
        ORDER BY group_id COLLATE BINARY
        ''',
        (request_id,),
    )
    group_offsets = {
        group_id: [decode_integer(old_hex), decode_integer(new_hex)]
        for group_id, old_hex, new_hex in group_rows
    }
    override_rows = db.execute(
        '''
        SELECT occurrence_id, old_offset_hex, new_offset_hex
        FROM edit_request_override_changes WHERE request_id = ?
        ORDER BY occurrence_id COLLATE BINARY
        ''',
        (request_id,),
    )
    overrides = {
        occurrence_id: [
            None if old_hex is None else decode_integer(old_hex),
            None if new_hex is None else decode_integer(new_hex),
        ]
        for occurrence_id, old_hex, new_hex in override_rows
    }
    return {
        'revision': stored.revision,
        'group_offsets': group_offsets,
        'overrides': overrides,
    }
