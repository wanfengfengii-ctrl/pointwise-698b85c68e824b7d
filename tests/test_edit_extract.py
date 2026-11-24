import sqlite3

from tests import DAY, NEXT_DAY, StoreTestCase, measurement
from tests.test_correction import install_commit_failure
from warehouse import (
    Observation,
    accept_batch,
    apply_extract,
    get_day_head,
    inspect_day,
    issue_edit_extract,
    issue_report,
    open_store,
    read_correction_audit,
    read_report,
    set_offsets,
)
from warehouse.calibration.overrides import OverrideChange
from warehouse.edits.schema import load_edit_extract_lines


def values(db, revision):
    return {row.occurrence_id: row.value for row in inspect_day(db, DAY, revision, 0).rows}


class EditExtractTests(StoreTestCase):
    def seed_triplet(self, *, lock_b=False):
        return accept_batch(
            self.db,
            'morning',
            DAY,
            [
                Observation('A', 'G', 'probe', 100, False),
                Observation('B', 'G', 'probe', 100, not lock_b),
                Observation('C', 'G', 'probe', 100, True),
            ],
        )

    def test_issue_freeze_and_ordinary_ids_keep_integer_interface(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['C', 'A', 'C'])
        self.assertIsInstance(extract['extract_id'], str)
        self.assertTrue(extract['extract_id'])
        self.assertEqual(extract['lines'], [['probe', 100], ['probe', 100], ['probe', 100]])
        frozen = load_edit_extract_lines(self.db, extract['extract_id'])
        self.assertEqual(
            [(line.position, line.occurrence_id, line.label, line.value) for line in frozen],
            [(0, 'C', 'probe', 100), (1, 'A', 'probe', 100), (2, 'C', 'probe', 100)],
        )
        # Ordinary reports still use the V1 integer interface.
        ordinary = issue_report(self.db, DAY, first, ['C', 'A', 'C'])
        self.assertIsInstance(ordinary.report_id, int)
        self.assertEqual(read_report(self.db, ordinary.report_id).revision, first)
        # Issuing does not move the day head.
        self.assertEqual(get_day_head(self.db, DAY), first)

    def test_apply_locked_target_via_group_offset_protects_group(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['C', 'A', 'C'])
        receipt = apply_extract(self.db, 'req-1', extract['extract_id'], [(1, 110)])
        self.assertEqual(receipt['group_offsets'], {'G': [0, 10]})
        # Missing overrides and explicit zero are distinct in the receipt.
        self.assertEqual(receipt['overrides'], {'B': [None, 0], 'C': [None, 0]})
        head = get_day_head(self.db, DAY)
        self.assertEqual(receipt['revision'], head)
        self.assertEqual(values(self.db, head), {'A': 110, 'B': 100, 'C': 100})
        audit = read_correction_audit(self.db, head)
        self.assertEqual(audit.group_offsets, (('G', 10),))
        self.assertEqual(
            audit.overrides, (OverrideChange('B', 0), OverrideChange('C', 0))
        )

    def test_replay_returns_first_receipt_after_head_advance_and_reopen(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['C', 'A', 'C'])
        receipt = apply_extract(self.db, 'req-1', extract['extract_id'], [(1, 110)])
        advanced = set_offsets(self.db, DAY, receipt['revision'])
        # Submission order and duplicate items are ignored on replay.
        replayed = apply_extract(
            self.db,
            'req-1',
            extract['extract_id'],
            [(1, 110), (1, 110)],
        )
        self.assertEqual(replayed, receipt)
        self.reopen()
        replayed_after_reopen = apply_extract(
            self.db, 'req-1', extract['extract_id'], [(1, 110)]
        )
        self.assertEqual(replayed_after_reopen, receipt)
        # A replay publishes nothing and leaves the head alone.
        self.assertEqual(get_day_head(self.db, DAY), advanced)
        # The first correction revision and ordinary extracts remain readable.
        self.assertEqual(
            values(self.db, receipt['revision']),
            {'A': 110, 'B': 100, 'C': 100},
        )

    def test_locked_occurrence_with_locked_neighbor_is_unsolvable(self):
        first = self.seed_triplet(lock_b=True)
        extract_a = issue_edit_extract(self.db, first, ['A'])
        extract_c = issue_edit_extract(self.db, first, ['C'])
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'a', extract_a['extract_id'], [(0, 110)])
        self.assertEqual(get_day_head(self.db, DAY), first)
        receipt = apply_extract(self.db, 'c', extract_c['extract_id'], [(0, 110)])
        self.assertEqual(receipt['group_offsets'], {})
        self.assertEqual(receipt['overrides'], {'C': [None, 10]})
        self.assertEqual(values(self.db, receipt['revision']),
                         {'A': 100, 'B': 100, 'C': 110})

    def test_conflicting_targets_rejected(self):
        first = self.seed_triplet(lock_b=True)
        extract = issue_edit_extract(self.db, first, ['C', 'C'])
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'x', extract['extract_id'], [(0, 110), (1, 105)])
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'y', extract['extract_id'], [(0, 110), (0, 105)])
        self.assertEqual(get_day_head(self.db, DAY), first)

    def test_request_id_content_fingerprint_uses_position_map(self):
        first = self.seed_triplet(lock_b=True)
        extract = issue_edit_extract(self.db, first, ['C', 'C'])
        receipt = apply_extract(self.db, 'c', extract['extract_id'], [(0, 110)])
        # The same target on another position of the same occurrence is a
        # different request content and cannot reuse the request id.
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'c', extract['extract_id'], [(1, 110)])
        self.assertEqual(
            apply_extract(self.db, 'c', extract['extract_id'], [(0, 110)]), receipt
        )

    def test_late_locked_row_blocks_first_apply(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['A'])
        second = accept_batch(
            self.db, 'late', DAY, [Observation('D', 'G', 'probe', 100, False)]
        )
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'd', extract['extract_id'], [(0, 110)])
        self.assertEqual(get_day_head(self.db, DAY), second)
        self.assertEqual(values(self.db, second)['D'], 100)
        self.assertEqual(values(self.db, second)['A'], 100)

    def test_stale_current_value_rejects_first_apply(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['A'])
        second = set_offsets(self.db, DAY, first, {'G': 5})
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'stale', extract['extract_id'], [(0, 110)])
        self.assertEqual(get_day_head(self.db, DAY), second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edit_requests').fetchone()[0], 0)

    def test_noop_correction_still_publishes_revision_with_empty_maps(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['A'])
        before_head = get_day_head(self.db, DAY)
        receipt = apply_extract(self.db, 'noop', extract['extract_id'], [(0, 100)])
        self.assertGreater(receipt['revision'], before_head)
        self.assertEqual(receipt['group_offsets'], {})
        self.assertEqual(receipt['overrides'], {})
        head = get_day_head(self.db, DAY)
        self.assertEqual(head, receipt['revision'])
        audit = read_correction_audit(self.db, head)
        self.assertEqual(audit.group_offsets, ())
        self.assertEqual(audit.overrides, ())
        self.assertEqual(values(self.db, head), {'A': 100, 'B': 100, 'C': 100})

    def test_existing_override_cleared_when_group_covers_target(self):
        first = self.seed_triplet()
        second = set_offsets(self.db, DAY, first, {'G': 10}, {'B': 7})
        self.assertEqual(values(self.db, second), {'A': 110, 'B': 107, 'C': 110})
        extract = issue_edit_extract(self.db, second, ['A'])
        # Keep A at its current value: the group offset already delivers it and
        # no group moves, so B's override and C's group inheritance both hold.
        receipt = apply_extract(self.db, 'keep', extract['extract_id'], [(0, 110)])
        self.assertEqual(receipt['group_offsets'], {})
        self.assertEqual(receipt['overrides'], {})
        self.assertEqual(
            values(self.db, receipt['revision']),
            {'A': 110, 'B': 107, 'C': 110},
        )
        audit = read_correction_audit(self.db, receipt['revision'])
        self.assertEqual(audit.group_offsets, ())
        self.assertEqual(audit.overrides, ())

    def test_override_change_receipt_distinguishes_clear_from_zero(self):
        first = self.seed_triplet()
        second = set_offsets(self.db, DAY, first, {'G': 10}, {'C': 3})
        self.assertEqual(values(self.db, second), {'A': 110, 'B': 110, 'C': 103})
        extract = issue_edit_extract(self.db, second, ['A'])
        receipt = apply_extract(self.db, 'mix', extract['extract_id'], [(0, 120)])
        # Group G moves 10 -> 20 for locked A. B currently inherits the group
        # offset and keeps 110, which now needs an explicit override 10. C is
        # unselected and its existing override 3 already preserves its 103, so
        # the write set leaves C alone.
        self.assertEqual(receipt['group_offsets'], {'G': [10, 20]})
        self.assertEqual(receipt['overrides'], {'B': [None, 10]})
        self.assertEqual(
            values(self.db, receipt['revision']),
            {'A': 120, 'B': 110, 'C': 103},
        )
        audit = read_correction_audit(self.db, receipt['revision'])
        self.assertEqual(audit.overrides, (OverrideChange('B', 10),))

    def test_override_clear_recorded_as_none_distinct_from_zero(self):
        first = self.seed_triplet()
        # B holds an explicit override 10; then locked A drives G to 10, which
        # makes B's override redundant: it is cleared and the receipt records
        # None (missing/cleared), never conflated with an explicit zero.
        second = set_offsets(self.db, DAY, first, {'G': 10}, {'B': 10})
        self.assertEqual(values(self.db, second), {'A': 110, 'B': 110, 'C': 110})
        extract = issue_edit_extract(self.db, second, ['A'])
        receipt = apply_extract(self.db, 'clear', extract['extract_id'], [(0, 110)])
        self.assertEqual(receipt['group_offsets'], {})
        self.assertEqual(receipt['overrides'], {'B': [10, None]})
        head = get_day_head(self.db, DAY)
        self.assertEqual(read_correction_audit(self.db, head).overrides,
                         (OverrideChange('B', None),))
        self.assertEqual(values(self.db, head), {'A': 110, 'B': 110, 'C': 110})
        self.reopen()
        self.assertEqual(
            apply_extract(self.db, 'clear', extract['extract_id'], [(0, 110)]),
            receipt,
        )

    def test_unselected_other_group_is_untouched(self):
        first = accept_batch(
            self.db,
            'morning',
            DAY,
            [
                Observation('A', 'G', 'probe', 100, False),
                Observation('H1', 'H', 'probe', 100, True),
            ],
        )
        extract = issue_edit_extract(self.db, first, ['A'])
        receipt = apply_extract(self.db, 'g', extract['extract_id'], [(0, 110)])
        self.assertEqual(receipt['group_offsets'], {'G': [0, 10]})
        self.assertEqual(receipt['overrides'], {})
        self.assertEqual(
            values(self.db, receipt['revision']), {'A': 110, 'H1': 100}
        )

    def test_exact_large_integers_survive_round_trip(self):
        big = 9223372036854775808
        first = accept_batch(
            self.db,
            'wide',
            DAY,
            [
                Observation('A', 'G', 'probe', big, False),
                Observation('B', 'G', 'probe', big, True),
            ],
        )
        extract = issue_edit_extract(self.db, first, ['A', 'B'])
        receipt = apply_extract(
            self.db,
            'big',
            extract['extract_id'],
            [(0, big + 10), (1, big - 5)],
        )
        self.assertEqual(receipt['group_offsets'], {'G': [0, 10]})
        self.assertEqual(receipt['overrides'], {'B': [None, -5]})
        head = get_day_head(self.db, DAY)
        self.assertEqual(
            values(self.db, head), {'A': big + 10, 'B': big - 5}
        )
        self.reopen()
        self.assertEqual(
            apply_extract(self.db, 'big', extract['extract_id'],
                          [(1, big - 5), (0, big + 10)]),
            receipt,
        )

    def test_failed_commit_leaves_no_artifact_and_retry_succeeds(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['A'])
        install_commit_failure(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            apply_extract(self.db, 'fail', extract['extract_id'], [(0, 110)])
        self.assertFalse(self.db.in_transaction)
        self.reopen()
        self.assertEqual(get_day_head(self.db, DAY), first)
        self.assertEqual(values(self.db, first), {'A': 100, 'B': 100, 'C': 100})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edit_requests').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            read_correction_audit(self.db, first + 1)
        receipt = apply_extract(self.db, 'fail', extract['extract_id'], [(0, 110)])
        self.assertEqual(receipt['revision'], first + 1)
        self.reopen()
        self.assertEqual(
            apply_extract(self.db, 'fail', extract['extract_id'], [(0, 110)]),
            receipt,
        )

    def test_invalid_issue_inputs(self):
        first = self.seed_triplet()
        bad_revisions = (0, -1, first + 1, 1.5, True, '1', None)
        for revision in bad_revisions:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                issue_edit_extract(self.db, revision, ['A'])
        bad_selections = ([], 'A', None, {})
        for selection in bad_selections:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                issue_edit_extract(self.db, first, selection)
        with self.assertRaises(ValueError):
            issue_edit_extract(self.db, first, ['A', 'missing'])
        other_day = accept_batch(self.db, 'other', NEXT_DAY, [measurement('Z')])
        with self.assertRaises(ValueError):
            issue_edit_extract(self.db, other_day, ['A'])

    def test_invalid_apply_inputs(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['C', 'A', 'C'])
        eid = extract['extract_id']
        invalid = (
            ('', eid, [(1, 110)]),
            (123, eid, [(1, 110)]),
            (None, eid, [(1, 110)]),
            ('r', '', [(1, 110)]),
            ('r', 'missing', [(1, 110)]),
            ('r', eid, []),
            ('r', eid, [(1,)]),
            ('r', eid, [('1', 110)]),
            ('r', eid, [(True, 110)]),
            ('r', eid, [(1, 110.0)]),
            ('r', eid, [(1, True)]),
            ('r', eid, (1, 110)),
            ('r', eid, 'x'),
        )
        for request_id, target_extract, edits in invalid:
            with self.subTest(request_id=request_id, edits=edits), self.assertRaises(ValueError):
                apply_extract(self.db, request_id, target_extract, edits)
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'r', eid, [(3, 110)])
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'r', eid, [(-1, 110)])
        self.assertEqual(get_day_head(self.db, DAY), first)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM edit_requests').fetchone()[0], 0)

    def test_second_request_on_same_extract_applies_against_current_head(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['C'])
        first_receipt = apply_extract(self.db, 'one', extract['extract_id'], [(0, 110)])
        self.assertEqual(
            values(self.db, first_receipt['revision']),
            {'A': 100, 'B': 100, 'C': 110},
        )
        # The extract still freezes C at 100; a second request must fail the
        # first-write current-value check rather than silently re-issue work.
        with self.assertRaises(ValueError):
            apply_extract(self.db, 'two', extract['extract_id'], [(0, 120)])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM edit_requests WHERE request_id = 'two'"
            ).fetchone()[0],
            0,
        )
        # A fresh extract at the new head allows another independent correction.
        head = get_day_head(self.db, DAY)
        fresh = issue_edit_extract(self.db, head, ['C'])
        second_receipt = apply_extract(
            self.db, 'two', fresh['extract_id'], [(0, 120)]
        )
        self.assertEqual(
            values(self.db, second_receipt['revision']),
            {'A': 100, 'B': 100, 'C': 120},
        )

    def test_extract_survives_reinitialize(self):
        first = self.seed_triplet()
        extract = issue_edit_extract(self.db, first, ['A'])
        self.reopen()
        from warehouse import initialize_store
        initialize_store(self.db)
        receipt = apply_extract(self.db, 'r', extract['extract_id'], [(0, 110)])
        self.assertEqual(
            values(self.db, receipt['revision']),
            {'A': 110, 'B': 100, 'C': 100},
        )
