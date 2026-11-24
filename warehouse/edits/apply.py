"""Apply rewritable extracts and publish formal correction revisions."""

from collections.abc import Sequence
from sqlite3 import Connection

from ..calibration.audit import record_explicit_correction
from ..ingest.source_keys import validate_source_key
from ..ledger.day_index import advance_day_head, get_day_head
from ..ledger.history import load_day_facts
from ..ledger.revisions import append_revision
from ..ledger.transactions import atomic_write
from ..quality.effective import resolve_effective_offset
from .planning import build_correction_plan
from .schema import (
    FrozenPosition,
    load_edit_extract_header,
    load_edit_extract_lines,
    load_request_receipt,
    load_stored_request,
    make_fingerprint,
    save_request_receipt,
)


def _parse_edits(edits: object, position_count: int) -> dict[int, int]:
    if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes, bytearray)):
        raise ValueError('edits must be an ordered sequence of (position, target) pairs')
    if len(edits) == 0:
        raise ValueError('edits must be nonempty')

    targets_by_position: dict[int, int] = {}
    for item in edits:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)) or len(item) != 2:
            raise ValueError('each edit must be a (position, target) pair')
        position, target = item
        # Booleans are integers in Python but are explicitly not accepted.
        if type(position) is not int:
            raise ValueError('position must be an integer position inside the extract')
        if type(target) is not int:
            raise ValueError('target must be a signed Python integer')
        if not 0 <= position < position_count:
            raise ValueError(f'position {position} is outside the extract')
        previous = targets_by_position.get(position)
        if previous is not None and previous != target:
            raise ValueError(f'position {position} received conflicting targets')
        targets_by_position[position] = target
    return targets_by_position


def apply_extract(
    db: Connection, request_id: object, extract_id: object, edits: object
) -> dict:
    request_id = validate_source_key(request_id, 'request_id')
    if not isinstance(extract_id, str) or not extract_id:
        raise ValueError('extract_id must be a nonempty string')

    with atomic_write(db):
        header = load_edit_extract_header(db, extract_id)
        positions = load_edit_extract_lines(db, extract_id)
        targets_by_position = _parse_edits(edits, len(positions))
        fingerprint = make_fingerprint(targets_by_position)

        # The same occurrence behind several positions must agree on one target.
        targets_by_occurrence: dict[str, int] = {}
        for position in sorted(targets_by_position):
            frozen: FrozenPosition = positions[position]
            target = targets_by_position[position]
            earlier = targets_by_occurrence.get(frozen.occurrence_id)
            if earlier is not None and earlier != target:
                raise ValueError(
                    f'occurrence {frozen.occurrence_id!r} received conflicting targets'
                )
            targets_by_occurrence[frozen.occurrence_id] = target

        stored = load_stored_request(db, request_id)
        if stored is not None:
            # Replays return the first receipt; any other use of the request id
            # is rejected and leaves no new artifact.
            if stored.extract_id != extract_id or stored.fingerprint != fingerprint:
                raise ValueError(f'request_id {request_id!r} is already used for different content')
            return load_request_receipt(db, request_id)

        head = get_day_head(db, header.day)
        if head is None:
            raise ValueError(f'no published day head for {header.day}')
        facts = load_day_facts(db, header.day, head)
        current_rows = {row.occurrence_id: row for row in facts.observations}

        # First application: every submitted position must still hold the value
        # frozen when the extract was issued.
        for position in sorted(targets_by_position):
            frozen = positions[position]
            current = current_rows.get(frozen.occurrence_id)
            if current is None:
                raise ValueError(
                    f'occurrence {frozen.occurrence_id!r} is no longer visible'
                )
            current_value = current.raw_value + resolve_effective_offset(
                current, facts.group_offsets, facts.overrides
            )
            if current_value != frozen.value:
                raise ValueError(
                    f'current value of {frozen.occurrence_id!r} no longer matches the extract'
                )

        plan = build_correction_plan(
            facts.observations,
            facts.group_offsets,
            facts.overrides,
            targets_by_occurrence,
        )

        revision = append_revision(db, header.day, 'correction')
        # Reuse the V1 correction event machinery so fixed reads and the
        # correction audit account for exactly the offsets being published.
        record_explicit_correction(
            db, header.day, revision, plan.group_updates, plan.override_changes
        )
        advance_day_head(db, header.day, revision)
        save_request_receipt(
            db,
            request_id,
            extract_id,
            revision,
            fingerprint,
            plan.group_receipt,
            plan.override_receipt,
        )
        return {
            'revision': revision,
            'group_offsets': plan.group_receipt,
            'overrides': plan.override_receipt,
        }
