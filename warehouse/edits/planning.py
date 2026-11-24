"""Solve legal calibration plans that satisfy edit extract targets.

Writables are group offsets and, only on override-eligible rows, absolute
overrides. A locked (non-overridable) row always inherits its group offset, so
a selected locked occurrence can only be moved by changing that group's offset;
that change also lands on every other locked row in the group, which makes the
request unsolvable when any of them must keep a different value. An
override-eligible selected occurrence is instead corrected through an absolute
override, leaving the group offset and all other rows untouched.
"""

from dataclasses import dataclass

from ..calibration.eligibility import can_override
from ..calibration.overrides import OverrideChange
from ..ingest.observations import Observation
from ..quality.effective import resolve_effective_offset


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    group_updates: tuple[tuple[str, int], ...]
    override_changes: tuple[OverrideChange, ...]
    group_receipt: dict[str, list[int]]
    override_receipt: dict[str, list[int | None]]


def build_correction_plan(
    observations: tuple[Observation, ...],
    current_group_offsets: dict[str, int],
    current_overrides: dict[str, int],
    targets_by_occurrence: dict[str, int],
) -> CorrectionPlan:
    """Find any legal plan satisfying targets while preserving every other row.

    Unlisted occurrences must keep their current effective values, including
    rows accepted after the extract was issued.
    """
    rows = {row.occurrence_id: row for row in observations}
    current_offset = {
        occurrence_id: resolve_effective_offset(
            row, current_group_offsets, current_overrides
        )
        for occurrence_id, row in rows.items()
    }

    def required_offset(occurrence_id: str) -> int:
        row = rows[occurrence_id]
        if occurrence_id in targets_by_occurrence:
            return targets_by_occurrence[occurrence_id] - row.raw_value
        # Unlisted occurrences keep their current effective values.
        return current_offset[occurrence_id]

    # Locked rows cannot be overridden: every locked row in a group pins the
    # group offset, so their required offsets must agree.
    forced_group_offsets: dict[str, int] = {}
    for row in observations:
        if not can_override(row):
            forced = required_offset(row.occurrence_id)
            existing = forced_group_offsets.get(row.group_id)
            if existing is not None and existing != forced:
                raise ValueError(
                    f'no legal correction can satisfy occurrence {row.occurrence_id!r}'
                )
            forced_group_offsets[row.group_id] = forced

    def new_group_offset(group_id: str) -> int:
        return forced_group_offsets.get(group_id, current_group_offsets.get(group_id, 0))

    group_updates: list[tuple[str, int]] = []
    group_receipt: dict[str, list[int]] = {}
    for group_id, new_offset in forced_group_offsets.items():
        old_offset = current_group_offsets.get(group_id, 0)
        if old_offset != new_offset:
            group_updates.append((group_id, new_offset))
            group_receipt[group_id] = [old_offset, new_offset]
    group_updates.sort()

    override_changes: list[OverrideChange] = []
    override_receipt: dict[str, list[int | None]] = {}
    for row in observations:
        if not can_override(row):
            # The group offset is the only lever; verify the forced plan holds.
            if new_group_offset(row.group_id) != required_offset(row.occurrence_id):
                raise ValueError(
                    f'no legal correction can satisfy occurrence {row.occurrence_id!r}'
                )
            continue

        group_offset = new_group_offset(row.group_id)
        needed = required_offset(row.occurrence_id)
        if group_offset == needed:
            # The group alone yields the required value: drop any override so
            # this row resolves through the group offset.
            if row.occurrence_id in current_overrides:
                override_changes.append(OverrideChange(row.occurrence_id, None))
                override_receipt[row.occurrence_id] = [
                    current_overrides[row.occurrence_id],
                    None,
                ]
        else:
            existing = current_overrides.get(row.occurrence_id)
            if existing != needed:
                override_changes.append(OverrideChange(row.occurrence_id, needed))
                override_receipt[row.occurrence_id] = [existing, needed]
    override_changes.sort(key=lambda change: change.occurrence_id)

    return CorrectionPlan(
        tuple(group_updates),
        tuple(override_changes),
        dict(sorted(group_receipt.items())),
        dict(sorted(override_receipt.items())),
    )
