"""
Tests for the PWA service worker (August 2026) - added so PWABuilder/app
store packaging can detect a real, installable service worker.

Served at /sw.js (not /static/sw.js) specifically so its default scope is
the whole site ('/') rather than being confined to /static/ - a service
worker's max allowed scope is the directory containing the script it was
registered from. Deliberately narrow in what it caches: only a same-origin
GET under /static/ ever goes through the cache-first path. Everything else
- every page navigation, /api/*, /login, /settings/*, /stripe/webhook,
/admin/*, etc. - is left completely untouched, since a generic cache-first
rule must never be allowed to catch an API response or a webhook.
"""
import re


APP_TEMPLATES = [
    "actions.html", "calendar.html", "flow.html", "forecast.html",
    "import.html", "index.html", "manage.html", "settings.html",
    "transactions.html",
]


class TestServiceWorkerRoute:
    def test_sw_js_serves_at_root(self, auth_client):
        resp = auth_client.get("/sw.js")
        assert resp.status_code == 200

    def test_sw_js_content_type_is_javascript(self, auth_client):
        resp = auth_client.get("/sw.js")
        assert "javascript" in resp.headers.get("Content-Type", "")

    def test_sw_js_accessible_without_login(self, app):
        """The public landing page registers it too, so it must not
        require auth."""
        c = app.test_client()
        resp = c.get("/sw.js")
        assert resp.status_code == 200

    def test_sw_js_content_matches_static_file(self, auth_client):
        with open("static/sw.js", encoding="utf-8") as f:
            expected = f.read()
        resp = auth_client.get("/sw.js")
        assert resp.get_data(as_text=True) == expected


class TestServiceWorkerCacheScopeGuards:
    """Content-based checks on the served script's own guard clauses -
    the actual browser fetch-interception behaviour can't be exercised
    from pytest, so this asserts the source contains the specific
    exclusions the brief requires, the same way other JS-logic-only
    features in this codebase are checked."""

    def setup_method(self):
        with open("static/sw.js", encoding="utf-8") as f:
            self.src = f.read()

    def test_non_get_requests_are_excluded(self):
        assert "req.method !== 'GET'" in self.src

    def test_cross_origin_requests_are_excluded(self):
        assert "url.origin !== self.location.origin" in self.src

    def test_only_static_path_is_handled(self):
        assert "url.pathname.indexOf('/static/') !== 0" in self.src

    def test_has_install_and_fetch_handlers(self):
        assert "addEventListener('install'" in self.src
        assert "addEventListener('fetch'" in self.src
        assert "addEventListener('activate'" in self.src

    def test_precache_list_has_no_api_or_page_routes(self):
        """Guards against someone later adding a real page/API route to
        the precache list, which would defeat the whole point of scoping
        this to /static/ only. Scoped to just the PRECACHE_URLS array
        (not the whole file) so quoted paths inside comments elsewhere
        don't produce false positives."""
        array_match = re.search(r"PRECACHE_URLS\s*=\s*\[(.*?)\]", self.src, re.DOTALL)
        assert array_match, "PRECACHE_URLS array not found"
        entries = re.findall(r"'([^']*)'", array_match.group(1))
        assert len(entries) > 0
        assert all(e.startswith("/static/") for e in entries)


class TestServiceWorkerRegistration:
    def test_registered_on_landing_page(self, app):
        c = app.test_client()
        resp = c.get("/")
        body = resp.get_data(as_text=True)
        assert 'navigator.serviceWorker.register("/sw.js")' in body

    def test_landing_page_does_not_use_static_scoped_registration(self, app):
        c = app.test_client()
        resp = c.get("/")
        body = resp.get_data(as_text=True)
        assert "/static/sw.js" not in body

    def test_every_app_template_registers_root_scoped_sw(self):
        for name in APP_TEMPLATES:
            with open(f"templates/{name}", encoding="utf-8") as f:
                content = f.read()
            assert 'navigator.serviceWorker.register("/sw.js")' in content, \
                f"{name} does not register the root-scoped service worker"
            assert "/static/sw.js" not in content, \
                f"{name} still references the old /static/sw.js scope"
