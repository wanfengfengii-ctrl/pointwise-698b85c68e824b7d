"""Issue rewritable edit extracts with frozen positions."""

from collections.abc import Sequence
from sqlite3 import Connection

from ..ledger.revisions import require_revision
from ..ledger.transactions import atomic_write
from ..quality.daily import read_effective_day
from ..reports.selection import resolve_selection
from .schema import FrozenPosition, save_edit_extract


def issue_edit_extract(
    db: Connection, revision: int, occurrence_ids: Sequence[str]
) -> dict:
    # require_revision rejects booleans and unknown or non-positive revisions.
    require_revision(db, revision)
    if not isinstance(occurrence_ids, Sequence) or isinstance(
        occurrence_ids, (str, bytes, bytearray)
    ):
        raise ValueError('occurrence_ids must be an ordered sequence')
    if len(occurrence_ids) == 0:
        raise ValueError('occurrence_ids must be nonempty')

    with atomic_write(db):
        published = require_revision(db, revision)
        rows = read_effective_day(db, published.day, revision)
        lines = resolve_selection(rows, occurrence_ids)
        by_id = {row.occurrence_id: row for row in rows}
        positions = tuple(
            FrozenPosition(
                position=index,
                occurrence_id=occurrence_id,
                label=lines[index].label,
                value=by_id[occurrence_id].value,
            )
            for index, occurrence_id in enumerate(occurrence_ids)
        )
        extract_id = save_edit_extract(db, published.day, revision, positions)
        return {
            'extract_id': extract_id,
            'lines': [[line.label, line.value] for line in lines],
        }
