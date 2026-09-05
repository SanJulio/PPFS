"""
Tests for Digital Asset Links verification (August 2026) - lets the
Android TWA package built via PWABuilder open spendara.co.uk without
browser UI, by proving domain ownership at the well-known path Google's
verifier fetches.
"""
import json


class TestAssetLinksRoute:
    def test_serves_at_exact_well_known_path(self, auth_client):
        resp = auth_client.get("/.well-known/assetlinks.json")
        assert resp.status_code == 200

    def test_content_type_is_application_json(self, auth_client):
        resp = auth_client.get("/.well-known/assetlinks.json")
        assert resp.headers.get("Content-Type") == "application/json"

    def test_accessible_without_login(self, app):
        """Google's verifier crawler fetches this unauthenticated - it
        must not require a session."""
        c = app.test_client()
        resp = c.get("/.well-known/assetlinks.json")
        assert resp.status_code == 200

    def test_content_matches_static_file_and_is_valid_json(self, auth_client):
        with open("static/assetlinks.json", encoding="utf-8") as f:
            expected_raw = f.read()
        resp = auth_client.get("/.well-known/assetlinks.json")
        assert resp.get_data(as_text=True) == expected_raw

        data = json.loads(expected_raw)
        assert data[0]["relation"] == ["delegate_permission/common.handle_all_urls"]
        assert data[0]["target"]["namespace"] == "android_app"
        assert data[0]["target"]["package_name"] == "uk.co.spendara.twa"
        assert data[0]["target"]["sha256_cert_fingerprints"] == [
            "67:36:24:EC:3F:20:0C:31:2B:FB:2E:65:2B:FC:14:A0:11:8F:B6:B8:58:85:54:1A:95:69:15:78:08:2E:EB:DC"
        ]
