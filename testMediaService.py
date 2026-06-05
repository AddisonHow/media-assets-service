import io
import time
import pytest
import sys
from pathlib import Path
 
# Allow importing media_service from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
 
from mediaService import app, ASSETS_DIR, _cache, _cache_lock
 
 
# ── Fixtures ───────────────────────────────────────────────────────────────────
 
@pytest.fixture(autouse=True)
def clear_cache():
    """Wipe the in-memory cache before every test for isolation."""
    with _cache_lock:
        _cache.clear()
    yield
    with _cache_lock:
        _cache.clear()
 
 
@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
 
 
@pytest.fixture()
def sample_asset(tmp_path, monkeypatch):
    """
    Create a temporary assets directory with one PNG and one JSON asset,
    then point the service at it.
    """
    assets = tmp_path / "assets"
    wildlife = assets / "wildlife"
    wildlife.mkdir(parents=True)
 
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100   # minimal fake PNG
    (wildlife / "hawk.png").write_bytes(png_bytes)
 
    sprites = assets / "game_sprites"
    sprites.mkdir()
    (sprites / "player_idle.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
 
    misc = assets / "misc"
    misc.mkdir()
    (misc / "config.json").write_bytes(b'{"version": 1}')
 
    # Redirect the module-level ASSETS_DIR
    import mediaService as ms
    monkeypatch.setattr(ms, "ASSETS_DIR", assets)
 
    return assets
 
 
# ── User Story 1: Load game asset ─────────────────────────────────────────────
 
class TestLoadAsset:
 
    def test_valid_asset_returns_200(self, client, sample_asset):
        """Given a valid asset_id, service returns 200 with file bytes."""
        resp = client.get("/assets/wildlife/hawk.png")
        assert resp.status_code == 200
        assert resp.data[:4] == b"\x89PNG"
 
    def test_valid_asset_has_correct_content_type(self, client, sample_asset):
        """PNG assets should be served as image/png."""
        resp = client.get("/assets/wildlife/hawk.png")
        assert "image/png" in resp.content_type
 
    def test_valid_json_asset_returns_200(self, client, sample_asset):
        """Non-image assets (JSON) should also be served correctly."""
        resp = client.get("/assets/misc/config.json")
        assert resp.status_code == 200
        assert "application/json" in resp.content_type
 
    def test_missing_asset_returns_404(self, client, sample_asset):
        """Given an asset_id that does not exist, service returns 404."""
        resp = client.get("/assets/wildlife/unicorn.png")
        assert resp.status_code == 404
        body = resp.get_json()
        assert body is not None
        assert "error" in body
        assert body["asset_id"] == "wildlife/unicorn.png"
 
    def test_404_response_is_json(self, client, sample_asset):
        """404 error body must be JSON (not HTML)."""
        resp = client.get("/assets/does/not/exist.png")
        assert resp.is_json
 
    def test_response_within_two_seconds(self, client, sample_asset):
        """
        Non-functional requirement: 99% of asset requests served within 2 seconds.
        This test checks that a single uncached request completes well under 2s.
        """
        start = time.time()
        resp = client.get("/assets/wildlife/hawk.png")
        elapsed = time.time() - start
        assert resp.status_code == 200
        assert elapsed < 2.0, f"Response took {elapsed:.3f}s, expected < 2s"
 
    def test_extension_auto_detection(self, client, sample_asset):
        """
        Requesting 'wildlife/hawk' without extension should resolve to hawk.png.
        """
        resp = client.get("/assets/wildlife/hawk")
        assert resp.status_code == 200
 
    def test_path_traversal_rejected(self, client, sample_asset):
        """Security: asset_ids that escape ASSETS_DIR must be rejected."""
        resp = client.get("/assets/../../etc/passwd")
        assert resp.status_code in (400, 404)
 
    def test_list_assets_returns_all(self, client, sample_asset):
        """GET /assets lists all asset files on disk."""
        resp = client.get("/assets")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "assets" in body
        ids = body["assets"]
        assert any("hawk.png" in a for a in ids)
        assert any("player_idle.png" in a for a in ids)
        assert any("config.json" in a for a in ids)
 
    def test_health_check(self, client):
        """GET /health returns 200 and status: ok."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
 
 
# ── User Story 2: Cache frequently used assets ────────────────────────────────
 
class TestCaching:
 
    def test_first_request_is_cache_miss(self, client, sample_asset):
        """First request for an asset should be a cache MISS."""
        resp = client.get("/assets/wildlife/hawk.png")
        assert resp.status_code == 200
        assert resp.headers.get("X-Cache") == "MISS"
 
    def test_second_request_is_cache_hit(self, client, sample_asset):
        """
        Acceptance criterion: after the first request, subsequent requests
        for the same asset are served from cache (X-Cache: HIT).
        """
        client.get("/assets/wildlife/hawk.png")   # warm the cache
        resp = client.get("/assets/wildlife/hawk.png")
        assert resp.status_code == 200
        assert resp.headers.get("X-Cache") == "HIT"
 
    def test_cached_response_is_faster(self, client, sample_asset):
        """
        Non-functional requirement: cached requests served under 200 ms.
        After warming, a HIT should be noticeably quick.
        """
        client.get("/assets/wildlife/hawk.png")   # warm
 
        start = time.time()
        resp = client.get("/assets/wildlife/hawk.png")
        elapsed = time.time() - start
 
        assert resp.headers.get("X-Cache") == "HIT"
        assert elapsed < 0.2, f"Cached response took {elapsed:.3f}s, expected < 200ms"
 
    def test_upload_invalidates_cache(self, client, sample_asset):
        """
        Acceptance criterion: when an asset is updated via POST, the old cache
        entry is invalidated and the next GET returns the new version.
        """
        # 1. Warm the cache with v1
        resp1 = client.get("/assets/wildlife/hawk.png")
        assert resp1.headers.get("X-Cache") == "MISS"
 
        # 2. Upload v2 (different bytes)
        v2_bytes = b"\x89PNG\r\n\x1a\n" + b"\xFF" * 100   # distinct from v1
        upload = client.post(
            "/assets/wildlife/hawk.png",
            data={"file": (io.BytesIO(v2_bytes), "hawk.png")},
            content_type="multipart/form-data",
        )
        assert upload.status_code == 201
 
        # 3. Next GET must be a MISS (cache was invalidated) and return new bytes
        resp2 = client.get("/assets/wildlife/hawk.png")
        assert resp2.headers.get("X-Cache") == "MISS"
        assert resp2.data == v2_bytes
 
    def test_upload_too_large_returns_413(self, client, sample_asset):
        """Files exceeding 10 MB must be rejected with 413."""
        big = io.BytesIO(b"x" * (10 * 1024 * 1024 + 1))
        resp = client.post(
            "/assets/misc/toobig.bin",
            data={"file": (big, "toobig.bin")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 413
 
    def test_upload_no_file_returns_400(self, client, sample_asset):
        """POST without a file field should return 400."""
        resp = client.post("/assets/misc/nope.png", data={},
                           content_type="multipart/form-data")
        assert resp.status_code == 400
 
    def test_different_assets_cached_independently(self, client, sample_asset):
        """Caching one asset should not affect the cache status of another."""
        client.get("/assets/wildlife/hawk.png")   # warm hawk only
 
        resp_hawk = client.get("/assets/wildlife/hawk.png")
        resp_sprite = client.get("/assets/game_sprites/player_idle.png")
 
        assert resp_hawk.headers.get("X-Cache") == "HIT"
        assert resp_sprite.headers.get("X-Cache") == "MISS"
