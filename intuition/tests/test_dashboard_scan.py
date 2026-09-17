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
    assert "MAX_CLIPBOARD_IMAGE_PX" in html
    assert "MAX_CLIPBOARD_IMAGE_BYTES" in html
    assert "MAX_CLIPBOARD_DATA_URL_CHARS" in html
    assert "MATERIAL_CHAT_TIMEOUT_MS" in html
    assert "materialSnapshotGeneration" in html
    assert "isCurrentRequest" in html
    assert "window.renderNeuralMath = renderNeuralMath" in html
    assert "if (sig === sigTodo) { renderNeuralMath(); return; }" in html
    assert "{left:'$', right:'$', display:false}" in html
    assert "materialChatQuestion').addEventListener('paste'" in html
    assert 'id="materialLasso"' not in html
    assert 'class="lassoLayer"' not in html
    assert "inline mathematics as \\( ... \\)" in dashboard.DRIVE_LEARNING_SYSTEM


def test_announcement_ticker_markup_and_wiring_is_present():
    """Announcements are a single scrolling ticker - no collapsible panel. The
    strip pauses on hover/focus (CSS) and a click opens the shared read popover."""
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    for marker in ('id="annTicker"', 'id="annTrack"', 'id="annBadge"',
                   'id="annPop"', 'function renderAnnouncements', 'data-ann-open='):
        assert marker in html, marker
    assert "@keyframes annMarquee" in html
    assert ".announcementRibbon:hover .annTrack" in html
    assert ":focus-within .annTrack { animation-play-state:paused; }" in html
    # The ticker must roll even under prefers-reduced-motion (slowed, not frozen):
    # no `animation:none` override on the track, speed chosen in JS.
    assert ".announcementRibbon .annTrack { animation: none; }" not in html
    assert "annReducedMotion.matches ? 60 : 105" in html
    # The collapsible panel and its controls are gone.
    for gone in ('id="annExpand"', 'id="annPanelBody"', 'id="annFilters"',
                 'id="annList"', 'id="annAdmin"', 'data-ann-filter=',
                 'data-ann-summarize=', 'intuition-ann-expanded'):
        assert gone not in html, gone
    # The read popover still marks items read on the server.
    assert "action: 'read'" in html
    assert "announcementCourseKey" in html
    # The server is still the source of truth - no browser-local store.
    assert "intuition-announcements'" not in html
    assert "localAnnouncements" not in html


def test_focus_bar_markup_and_wiring_is_present():
    """The Focus/Pomodoro bar must be real markup wired to /api/focus."""
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    for marker in ('id="focusBar"', 'id="focusClock"', 'id="focusPreset"',
                   'id="focusStart"', 'id="focusTask"', 'id="focusTrack"',
                   'id="focusCar"', 'id="focusTheme"', 'id="focusCircuitSel"',
                   'id="focusCircuitPath"', 'id="focusCircuitCar"', 'id="focusLap"',
                   'FOCUS_CIRCUITS', 'FOCUS_CIRCUIT_META', 'function focusCarFrame',
                   'function focusRaceMs', 'function renderFocus'):
        assert marker in html, marker
    assert "'/api/focus'" in html and "action:'log'" in html
    assert "renderFocus(s);" in html
    # Team radio: the "box, box" call before the pit stop is a full transmission
    # (beep + PTT click + static bed + squelch) with an on-screen transcript and
    # its own toggle - not the old bare two-note blip.
    for marker in ('id="focusRadioMsg"', 'id="focusRadio"', 'function focusRadioTx',
                   'FOCUS_BOX_CALLS', 'Box, box, box', 'focusPrimeRadio',
                   'FOCUS_RADIO_CHATTER', 'function focusMaybeChatter'):
        assert marker in html, marker
    # Leaving the pit box after a break plays the "vroom-off" pit-lane launch
    # instead of the standing-start lights.
    assert "function focusPitExit" in html
    assert "focusFromPit" in html
    # Optional focus-tone bed (off by default): brown noise / 40 Hz pulsed noise
    # / 40 Hz binaural, playing only through a focus stint.
    for marker in ('id="focusTone"', 'FOCUS_TONES', 'function focusStartBed',
                   'function focusSyncBed', 'createChannelMerger', '40 Hz binaural'):
        assert marker in html, marker
    # The car laps at real pace toward a full Grand Prix.
    assert "Grand Prix" in html
    assert '"class": "wheel"' not in html and 'class="wheel"' in html  # F1-car shape, not an arrow
    # Livery accents are colour-only and carry the non-affiliation notice;
    # circuit outlines are OSM-derived and attributed.
    assert ".focusBar.livery-ferrari" in html
    assert "not affiliated with formula 1" in html.lower()
    assert "openstreetmap" in html.lower()
    for circuit in ("monza", "monaco", "suzuka", "spa", "silverstone", "cota",
                    "bahrain", "marinabay", "hungaroring", "zandvoort", "redbullring"):
        assert f"{circuit}:" in html or f"{circuit}: " in html, circuit


def test_dashboard_html_is_source_not_a_browser_dom_snapshot():
    """Guards against re-introducing the corruption where someone saved the running
    page's serialized DOM over this file instead of copying the source: CodeMirror's
    runtime-generated class names (U+037C), browser-extension overlay nodes, and a
    runtime style attribute on <html> all leak in that way and none belong in source."""
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert "ͼ" not in html, "CodeMirror runtime style classes - this is a DOM snapshot"
    assert "codex-agent-overlay" not in html
    assert "aiFabShadowRoot" not in html
    assert 'style="--material-drawer-width' not in html
    assert html.lower().count("<!doctype html>") == 1
    assert html.lstrip().startswith("<!doctype html>\n<html lang=\"en\">\n<head>")


def test_dashboard_has_central_interface_failure_capture():
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert "reportInterfaceFailure" in html
    assert "unhandledrejection" in html
    assert "/api/client-log" in html
    assert "AbortController" in html
    assert "refreshInFlight" in html
    assert "authBusy" in html
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


def test_java_mode_has_writing_scaffolds_and_compiler_guidance():
    html_path = os.path.join(os.path.dirname(dashboard.__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as stream:
        html = stream.read()
    assert '"@codemirror/autocomplete"' in html
    assert 'id="labJavaCoachTab"' in html
    assert 'id="labJavaCoachPane"' in html
    assert "JAVA_COACH_STAGES" in html
    assert "JAVA_SNIPPETS" in html
    assert "javaDiagnosticFromText" in html
    assert "data-java-diagnostic-line" in html


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
