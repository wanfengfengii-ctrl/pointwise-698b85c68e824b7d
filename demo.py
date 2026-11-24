import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from warehouse import (
    Observation,
    accept_batch,
    apply_extract,
    find_batch,
    get_day_head,
    inspect_day,
    issue_edit_extract,
    issue_report,
    load_batch_payload,
    open_store,
    read_correction_audit,
    read_report,
    set_offsets,
)


DAY = '2026-04-15'


def require_result(actual, expected):
    if actual != expected:
        raise RuntimeError(f'expected {expected!r}, got {actual!r}')


def show_scenario(case_id, inputs, expected, actual, downstream_expected, downstream_actual,
                  code_paths, oracle):
    require_result(actual, expected)
    require_result(downstream_actual, downstream_expected)
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': inputs,
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': code_paths,
        'oracle': oracle,
    }, ensure_ascii=False))


def main():
    source = [Observation('B', 'G', 'B', 100, True), Observation('A', 'G', 'A', 100, False)]
    selection = ['B', 'A', 'B']
    with TemporaryDirectory(prefix='chengwan-demo-') as directory:
        path = Path(directory) / 'warehouse.sqlite3'
        with closing(open_store(path)) as db:
            first = accept_batch(db, 'morning', DAY, source)
            require_result(accept_batch(db, 'morning', DAY, source), first)
            header = find_batch(db, 'morning')
            require_result([row.occurrence_id for row in load_batch_payload(db, header).observations], ['B', 'A'])
            second = set_offsets(db, DAY, first, {'G': 10}, {'B': 2})
            before = inspect_day(db, DAY, first, 102)
            after = inspect_day(db, DAY, second, 102)
            require_result([row.raw_value for row in after.rows], [100, 100])
            require_result(before.groups[0].total, 200)
            show_scenario(
                'v03',
                {'day': DAY, 'source': [['B', 100, True], ['A', 100, False]],
                 'group_offsets': {'G': 10}, 'overrides': {'B': 2}, 'threshold': 102},
                {'old': [['A', 100], ['B', 100]], 'new': [['A', 110], ['B', 102]]},
                {'old': [[row.occurrence_id, row.value] for row in before.rows],
                 'new': [[row.occurrence_id, row.value] for row in after.rows]},
                {'groups': [['G', 212, 1]], 'candidates': ['A']},
                {'groups': [[group.group_id, group.total, group.exceedance_count] for group in after.groups],
                 'candidates': [row.occurrence_id for row in after.exceedances]},
                ['warehouse/calibration/updates.py', 'warehouse/ledger/history.py',
                 'warehouse/quality/effective.py', 'warehouse/quality/daily.py',
                 'warehouse/quality/exceedance.py', 'warehouse/quality/totals.py'],
                'A uses 100+10 and B uses 100+2. Only A is strictly above 102.',
            )
            issued = issue_report(db, DAY, second, selection)
            third = set_offsets(db, DAY, second, overrides={'B': None})

        with closing(open_store(path)) as db:
            reprinted = read_report(db, issued.report_id)
            show_scenario(
                'v06',
                {'day': DAY, 'source_revision': second, 'selection': selection, 'clear': 'B'},
                [['B', 102], ['A', 110], ['B', 102]],
                [[line.label, line.value] for line in issued.lines],
                {'source_revision': second, 'reprint': [['B', 102], ['A', 110], ['B', 102]],
                 'current': [110, 110]},
                {'source_revision': reprinted.revision,
                 'reprint': [[line.label, line.value] for line in reprinted.lines],
                 'current': [row.value for row in inspect_day(db, DAY, third, 102).rows]},
                ['warehouse/reports/selection.py', 'warehouse/reports/issuance.py',
                 'warehouse/reports/archive.py', 'warehouse/reports/reprint.py',
                 'warehouse/calibration/overrides.py', 'warehouse/quality/daily.py'],
                'Read 102, 110, 102 from the published second revision in the selected order. '
                'After clearing B, both current rows use the group offset of 10.',
            )


def show_edit_scenario():
    # Separate working store so the V1 scenarios above keep their exact data.
    with TemporaryDirectory(prefix='chengwan-demo-edit-') as directory:
        path = Path(directory) / 'edit-warehouse.sqlite3'
        with closing(open_store(path)) as db:
            first = accept_batch(db, 'morning', DAY, [
                Observation('A', 'G', 'probe', 100, False),
                Observation('B', 'G', 'probe', 100, True),
                Observation('C', 'G', 'probe', 100, True),
            ])
            issued = issue_edit_extract(db, first, ['C', 'A', 'C'])
            require_result(issued['lines'],
                           [['probe', 100], ['probe', 100], ['probe', 100]])
            receipt = apply_extract(db, 'req-A-110', issued['extract_id'], [(1, 110)])
            corrected = receipt['revision']
            corrected_day = inspect_day(db, DAY, corrected, 0)
            require_result(
                [[row.occurrence_id, row.value] for row in corrected_day.rows],
                [['A', 110], ['B', 100], ['C', 100]],
            )
            audit = read_correction_audit(db, corrected)
            require_result([(group, value) for group, value in audit.group_offsets],
                           [('G', 10)])
            # The day head continues after the correction; the extract binding
            # remains valid for replay afterwards.
            advanced = set_offsets(db, DAY, corrected)
            first_receipt = receipt

        with closing(open_store(path)) as db:
            replayed = apply_extract(db, 'req-A-110', issued['extract_id'], [(1, 110)])
            require_result(replayed, first_receipt)
            # Replays publish nothing and do not move the day head.
            head_after_replay = get_day_head(db, DAY)
            require_result(head_after_replay, advanced)
            # The first correction revision still reads the fixed corrected day.
            fixed = inspect_day(db, DAY, corrected, 0)
            require_result(
                [[row.occurrence_id, row.value] for row in fixed.rows],
                [['A', 110], ['B', 100], ['C', 100]],
            )
            ordinary = issue_report(db, DAY, first, ['A'])
            ordinary_reprint = [[line.label, line.value]
                                for line in read_report(db, ordinary.report_id).lines]
            require_result(ordinary_reprint, [['probe', 100]])
            fixed_table = [[row.occurrence_id, row.value] for row in fixed.rows]

        print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'edit-A-110',
        'input': {'day': DAY, 'selection': ['C', 'A', 'C'], 'edits': [[1, 110]]},
        'expected': {
            'receipt': {
                'revision': corrected,
                'group_offsets': {'G': [0, 10]},
                'overrides': {'B': [None, 0], 'C': [None, 0]},
            },
            'day_table': [['A', 110], ['B', 100], ['C', 100]],
        },
        'actual': {
            'receipt': first_receipt,
            'day_table': [[row.occurrence_id, row.value] for row in corrected_day.rows],
        },
        'downstream_expected': {
            'replay_receipt': first_receipt,
            'head_after_replay': advanced,
            'fixed_first_correction': [['A', 110], ['B', 100], ['C', 100]],
            'ordinary_reprint': [['probe', 100]],
        },
        'downstream_actual': {
            'replay_receipt': replayed,
            'head_after_replay': head_after_replay,
            'fixed_first_correction': fixed_table,
            'ordinary_reprint': ordinary_reprint,
        },
        'code_paths': ['warehouse/edits/issuance.py', 'warehouse/edits/apply.py',
                       'warehouse/edits/planning.py', 'warehouse/edits/schema.py',
                       'warehouse/calibration/audit.py', 'warehouse/quality/effective.py'],
        'oracle': (
            'Locked A can only move through group offset G 0->10. B and C are override-eligible, '
            'so the write set pins each with an explicit zero override; missing old overrides are '
            'reported as null, distinct from the new explicit zero. The actual write set therefore '
            'touches only the G group offset plus the B/C override rows, and every unlisted '
            'occurrence keeps its value even when its group moves. Replaying the same request id '
            'after the head advanced and after reopening returns the first receipt without a new '
            'revision.'
        ),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
    show_edit_scenario()
