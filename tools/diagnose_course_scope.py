"""Read-only diagnostic: why did semester-scope course sync return fewer courses
than you're actually enrolled in?

Lists every membership Learn's REST API returns, unfiltered, alongside the two
checks api.get_courses() applies (availability, parsed semester) - so you can see
exactly which ones get dropped and why, instead of just the final trimmed count.

    python -m tools.diagnose_course_scope [download_root]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intuition import auth, rest, semester
from intuition.api import is_course_excluded


def main():
    download_root = sys.argv[1] if len(sys.argv) > 1 else "NTU"
    token = auth.load_token()
    if not token:
        raise SystemExit("No saved BbRouter session - open the dashboard and "
                          "establish a session first, then rerun this.")

    memberships = rest._get_paged(
        token, rest.REST_MY_COURSES_URL,
        params={"expand": "course", "fields": "courseId,course"})

    now = semester.current_semester()
    print("Current semester (today): {}\n".format(semester.format_semester(now)))
    print("{:<45} {:<12} {:<8} {}".format(
        "COURSE NAME", "AVAILABLE", "SEMESTER", "IN SCOPE?"))
    print("-" * 90)

    kept = 0
    for m in memberships:
        course = m.get("course") or {}
        name = course.get("name") or course.get("courseId") or "?"
        available = (course.get("availability") or {}).get("available", "?")
        parsed = semester.parse_course_semester(name)
        parsed_str = semester.format_semester(parsed) if parsed else "(none)"
        excluded = is_course_excluded(name, download_root=download_root)
        disabled = available == "Disabled"
        in_scope = parsed == now and not excluded and not disabled
        if in_scope:
            kept += 1
        reason = ("EXCLUDED" if excluded else "DISABLED" if disabled
                  else "IN SCOPE" if in_scope
                  else "wrong semester" if parsed else "no semester in name")
        print("{:<45} {:<12} {:<8} {}".format(name[:45], available, parsed_str, reason))

    print("\n{} of {} membership(s) would be kept under semester scope.".format(
        kept, len(memberships)))


if __name__ == "__main__":
    main()
