import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from warehouse import Observation, accept_batch, inspect_day, issue_report, open_store, read_report, set_offsets


DAY = "2026-04-15"


def seed(db, *, lock_b=False):
    return accept_batch(db, "morning", DAY, [
        Observation("A", "G", "probe", 100, False),
        Observation("B", "G", "probe", 100, not lock_b),
        Observation("C", "G", "probe", 100, True),
    ])


def day_values(db, revision):
    return {row.occurrence_id: row.value for row in inspect_day(db, DAY, revision, 100).rows}


def require_equal(actual, expected):
    if actual != expected:
        raise RuntimeError(f"expected {expected!r}, got {actual!r}")


def main():
    with TemporaryDirectory(prefix="chengwan-edit-example-") as directory:
        path = Path(directory) / "warehouse.sqlite3"
        with closing(open_store(path)) as db:
            initial = seed(db)
            first = issue_report(db, DAY, initial, ["A"])
            second = issue_report(db, DAY, initial, ["C"])
            require_equal(first.lines, second.lines)
            manual = set_offsets(db, DAY, initial, {"G": 10})
            after = day_values(db, manual)
            require_equal(after, {"A": 110, "B": 110, "C": 110})

        with closing(open_store(path)) as db:
            reports = [read_report(db, report_id) for report_id in (first.report_id, second.report_id)]
            require_equal([r.revision for r in reports], [initial, initial])
            presentations = [[[line.label, line.value] for line in r.lines] for r in reports]
            require_equal(presentations, [[["probe", 100]], [["probe", 100]]])
            require_equal(day_values(db, manual), after)

    print(json.dumps({"ordinary_reprints": presentations, "group_only_day": after}, sort_keys=True))


if __name__ == "__main__":
    main()
