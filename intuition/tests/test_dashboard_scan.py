import threading
import os
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard


def test_static_assets_are_scoped_to_dashboard_directory():
    asset = dashboard.static_asset_path(
        "/static/vendor/katex/contrib/auto-render.min.js")
    assert asset and os.path.isfile(asset)
    assert dashboard.static_asset_path("/static/../dashboard.py") is None
    assert dashboard.static_asset_path("/api/state") is None
    assert dashboard.static_asset_path("/api/health") is None


def test_friday_output_is_included_in_katex_rendering():
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert ".todoFindingText, .materialMessage.assistant" in html
    assert "insertAdjacentHTML('beforeend'" in html
    assert "inline mathematics as \\( ... \\)" in dashboard.DRIVE_LEARNING_SYSTEM


def test_dashboard_has_central_interface_failure_capture():
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert "reportInterfaceFailure" in html
    assert "unhandledrejection" in html
    assert "/api/client-log" in html
    assert hasattr(dashboard.Handler, "_do_GET")
    assert hasattr(dashboard.Handler, "_do_PUT")
    assert hasattr(dashboard.Handler, "_do_POST")


def test_friday_output_cannot_widen_the_chat_panel():
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert ".materialMessage table { width:100%; min-width:0; table-layout:fixed;" in html
    assert ".materialChatLog { flex:1; min-width:0; min-height:0; overflow-y:auto; overflow-x:hidden;" in html
    assert ".materialMessage .katex-display { max-width:100%; overflow-x:auto;" in html


class FakeCache:
    def reset_stats(self): pass
    def save(self): pass
    def stats(self): return {"hits": 2, "misses": 3}


def _state():
    notes = []
    state = SimpleNamespace(
        cache=FakeCache(), courses=[{"name": "SC2001", "id": "1"},
                                   {"name": "SC2002", "id": "2"}],
        token="token", prefer_rest=True, download_root="root", ledger=None,
        lock=threading.Lock(), plan=[], skipped=[], scan_errors=[], scanning=True,
        note=notes.append, refresh_media=lambda: None,
    )
    return state, notes


def test_selected_course_nodes_flow_into_delta_manifest():
    state, notes = _state()
    plans = {
        "1": [{"path": "a.pdf", "status": "new"}],
        "2": [{"path": "b.pdf", "status": "archived"},
              {"path": "c.pdf", "status": "new"}],
    }

    def tree(_token, _name, course_id, **_kwargs):
        return {"course_id": course_id}

    with patch.object(dashboard, "get_download_dir", side_effect=tree), \
         patch.object(dashboard, "build_plan",
                      side_effect=lambda value, *_a, **_k: plans[value["course_id"]]):
        dashboard.do_scan(state, ["1", "2"])

    assert len(state.plan) == 3
    assert [row["course"] for row in state.plan] == ["SC2001", "SC2002", "SC2002"]
    assert state.scan_errors == []
    assert state.scanning is False
    assert "2 new" in notes[-1] and "1 archived" in notes[-1]


def test_plan_failure_is_visible_and_other_course_still_renders():
    state, _notes = _state()
    with patch.object(dashboard, "get_download_dir",
                      side_effect=lambda *_a, **_k: {"ok": True}), \
         patch.object(dashboard, "build_plan",
                      side_effect=[OSError("path too long"),
                                   [{"path": "good.pdf", "status": "new"}]]):
        dashboard.do_scan(state, ["1", "2"])

    assert len(state.plan) == 1
    assert state.plan[0]["course"] == "SC2002"
    assert len(state.scan_errors) == 1
    assert "SC2001" in state.scan_errors[0]
    assert "path too long" in state.scan_errors[0]


def test_announcement_sync_scheduler_polls_immediately_on_startup():
    # Regression test: a professor's post made while the app was closed used to
    # sit unsynced until the next fixed 07:00/23:00 window fired *after* the
    # app happened to be running again - the scheduler only ever waited for
    # that window and never synced on startup the way inbound mail does.
    notes = []
    state = SimpleNamespace(token="", note=notes.append)
    with patch.object(dashboard, "seconds_until_next_announcement_sync",
                       return_value=3600):
        thread = threading.Thread(target=dashboard.announcement_sync_scheduler,
                                  args=(state,), daemon=True)
        thread.start()
        thread.join(timeout=1)

    assert notes == ["Scheduled announcement sync skipped: no Blackboard session"]
