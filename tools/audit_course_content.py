"""Read-only live audit of iNTUition course authoring/storage forms."""
import json
import os
import sys
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intuition import auth, rest
from intuition.contentcache import ContentCache
from intuition.sync import build_plan


def audit(download_root="NTU"):
    token = auth.load_token()
    if not token:
        raise SystemExit("No saved BbRouter session")
    cache = ContentCache(download_root)
    rows = []
    for name, course_id in rest.get_courses(token, favorites_only=True):
        raw = rest._get_paged(  # same read-only listing used by the scanner
            token, rest.REST_COURSE_CONTENTS_URL.format(course_id=course_id),
            params={"recursive": "true", "limit": rest.PAGE_LIMIT})
        handlers = Counter((item.get("contentHandler") or {}).get("id", "unknown")
                           for item in raw)
        body_files, body_hosts = 0, Counter()
        for item in raw:
            for anchor in BeautifulSoup(item.get("body") or "", "lxml").find_all(
                    "a", href=True):
                host = urlparse(anchor["href"]).netloc.lower()
                if host:
                    body_hosts[host] += 1
                if "/bbcswebdav/" in anchor["href"]:
                    body_files += 1
        try:
            tree, skipped = rest.get_download_dir(token, name, course_id, cache=cache)
            plan = build_plan(tree, download_root)
            error = ""
        except Exception as exc:  # audit must finish the other courses
            plan, skipped, error = [], [], "{}: {}".format(type(exc).__name__, exc)
        auth_probes = []
        samples = {}
        for entry in plan:
            link = entry.get("predownload_link") or ""
            form = ("rest_attachment" if "/learn/api/public/" in link
                    else "signed_bbcswebdav" if "/bbcswebdav/" in link else "other")
            if link and form not in samples:
                samples[form] = link
        for form, link in samples.items():
            try:
                with_cookie = requests.head(link, cookies={"BbRouter": token},
                                            headers=rest._headers(token),
                                            allow_redirects=False, timeout=15).status_code
                anonymous = requests.head(link, allow_redirects=False,
                                          timeout=15).status_code
                auth_probes.append({"form": form, "with_cookie": with_cookie,
                                    "anonymous": anonymous})
            except requests.RequestException as exc:
                auth_probes.append({"form": form, "error": str(exc)})
        rows.append({
            "course": name, "id": course_id, "content_items": len(raw),
            "files_detected": len(plan), "embedded_bbcswebdav": body_files,
            "handlers": dict(handlers), "body_link_hosts": dict(body_hosts),
            "external_items_skipped": len(skipped), "error": error,
            "authorization_probes": auth_probes,
        })
    cache.save()
    return rows


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "NTU"
    print(json.dumps(audit(root), indent=2, sort_keys=True))
