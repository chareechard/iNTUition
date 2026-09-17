from datetime import date, timedelta
from unittest.mock import patch

import pytest

from intuition import focus as focus_mod
from intuition.focus import Store


def test_log_appends_and_persists_across_reload(tmp_path):
    store = Store(str(tmp_path))
    row = store.log(25, "focus", "  CE4057  lab   report ")
    assert row["id"] and row["kind"] == "focus"
    assert row["task"] == "CE4057 lab report"
    assert row["date"] == date.today().isoformat()

    again = Store(str(tmp_path))
    assert [s["id"] for s in again.sessions] == [row["id"]]
    assert again.snapshot()["total"] == 1


def test_snapshot_tally_excludes_breaks(tmp_path):
    store = Store(str(tmp_path))
    store.log(25, "focus")
    store.log(25, "focus")
    store.log(5, "short_break")
    store.log(15, "long_break")
    snap = store.snapshot()
    assert snap["today"] == 2
    assert snap["today_minutes"] == 50
    assert snap["total"] == 2
    assert snap["total_minutes"] == 50
    assert snap["week"] == 2 and snap["month"] == 2
    # 14-day series, last entry is today, breaks not counted.
    assert len(snap["days"]) == 14
    assert snap["days"][-1] == {"date": date.today().isoformat(), "count": 2, "minutes": 50}
    assert sum(d["minutes"] for d in snap["days"]) == 50


def test_snapshot_streak_and_task_breakdown(tmp_path):
    store = Store(str(tmp_path))

    def frozen(d):
        class F(date):
            @classmethod
            def today(cls):
                return d
        return F

    for offset in (2, 1, 0):                       # three days in a row
        with patch.object(focus_mod, "date", frozen(date.today() - timedelta(days=offset))):
            store.log(25, "focus", "CE4057 report")
    store.log(50, "focus", "MH1200 tutorial")      # today, second task
    snap = store.snapshot()
    assert snap["streak"] == 3
    # ranked by minutes: CE4057 (3 x 25) ahead of MH1200 (50)
    assert snap["tasks"][0] == {"task": "CE4057 report", "minutes": 75}
    assert {"task": "MH1200 tutorial", "minutes": 50} in snap["tasks"]


def test_snapshot_streak_survives_a_quiet_today(tmp_path):
    store = Store(str(tmp_path))

    class Yesterday(date):
        @classmethod
        def today(cls):
            return date.today() - timedelta(days=1)

    with patch.object(focus_mod, "date", Yesterday):
        store.log(25, "focus")
    # nothing logged "today" - streak should still report the run (grace day)
    assert store.snapshot()["streak"] == 1


def test_snapshot_today_is_date_scoped(tmp_path):
    store = Store(str(tmp_path))

    class FrozenYesterday(date):
        @classmethod
        def today(cls):
            return date(2020, 1, 1)

    with patch.object(focus_mod, "date", FrozenYesterday):
        store.log(30, "focus")
    store.log(25, "focus")  # real today
    snap = store.snapshot()
    assert snap["total"] == 2 and snap["total_minutes"] == 55
    assert snap["today"] == 1 and snap["today_minutes"] == 25


@pytest.mark.parametrize("bad", ["", None, "abc", 0, -5, 181, 999])
def test_log_rejects_bad_minutes(tmp_path, bad):
    with pytest.raises(ValueError):
        Store(str(tmp_path)).log(bad)


def test_log_coerces_unknown_kind_to_focus(tmp_path):
    store = Store(str(tmp_path))
    assert store.log(25, "sprint")["kind"] == "focus"


def test_rolling_cap_keeps_newest(tmp_path):
    store = Store(str(tmp_path))
    with patch.object(focus_mod, "MAX_ROWS", 3):
        for i in range(1, 6):
            store.log(i + 1, "focus")
        assert [s["minutes"] for s in store.sessions] == [4, 5, 6]
    assert Store(str(tmp_path)).snapshot()["total"] == 3


def test_corrupt_file_loads_empty(tmp_path):
    Store(str(tmp_path)).log(25, "focus")
    (tmp_path / ".intuition" / "focus.json").write_text("{not json", encoding="utf-8")
    assert Store(str(tmp_path)).sessions == []
