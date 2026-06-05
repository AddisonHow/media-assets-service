import os
import time
import hashlib
import mimetypes
import threading
from pathlib import Path
from flask import Flask, request, send_file, jsonify, abort
 
# configuration
 
ASSETS_DIR = Path(__file__).parent / "assets"
CACHE_TTL_SECONDS = 300          # Cache entries expire after 5 minutes
MAX_ASSET_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB upload limit
 
app = Flask(__name__)
 


 
_cache: dict = {}
_cache_lock = threading.Lock()
 
 
def _cache_get(asset_id: str):
    """Return cached (data, mimetype) if present and not expired, else None."""
    with _cache_lock:
        entry = _cache.get(asset_id)
        if entry is None:
            return None
        if time.time() - entry["cached_at"] > CACHE_TTL_SECONDS:
            del _cache[asset_id]
            return None
        return entry["data"], entry["mimetype"]
 
 
def _cache_set(asset_id: str, data: bytes, mimetype: str):

    #store asset bytes in cache
    with _cache_lock:
        _cache[asset_id] = {
            "data": data,
            "mimetype": mimetype,
            "cached_at": time.time(),
        }
 
 
def _cache_invalidate(asset_id: str):
    
    #remove single entry from cache
    with _cache_lock:
        _cache.pop(asset_id, None)
 
 
def _asset_path(asset_id: str) -> Path:

    #resolev asset id like 'wildlife/hawk' to a absolute path under assets_dir
    # Normalise: strip leading slashes so Path doesn't treat it as absolute
    safe_id = asset_id.lstrip("/")
    resolved = (ASSETS_DIR / safe_id).resolve()
 
    #secutiry-ensurs resolved path is still in assets_dir
    if not str(resolved).startswith(str(ASSETS_DIR.resolve())):
        raise ValueError("Invalid asset_id: path traversal detected")
 
    return resolved
 
 
def _find_asset_file(base_path: Path):
   
    #given a base path, retrun the file path, returns none if nothing is found 
    if base_path.exists() and base_path.is_file():
        return base_path
 
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                ".mp3", ".ogg", ".wav", ".json", ".txt"]:
        candidate = base_path.with_suffix(ext)
        if candidate.exists():
            return candidate
 
    return None
 
 
# routes
 
@app.route("/health", methods=["GET"])
def health():
    """Simple health-check endpoint."""
    return jsonify({"status": "ok", "service": "media-assets"}), 200
 
 
@app.route("/assets", methods=["GET"])
def list_assets():

    #return list of all asset ids curently on disk
    #asset ids use forward slash seperators
    asset_list = []
    for path in ASSETS_DIR.rglob("*"):
        if path.is_file():
            #express path relative to assets_dir with \
            rel = path.relative_to(ASSETS_DIR).as_posix()
            asset_list.append(rel)
 
    return jsonify({"assets": sorted(asset_list), "count": len(asset_list)}), 200
 
 
@app.route("/assets/<path:asset_id>", methods=["GET"])
def get_asset(asset_id: str):
    """
    Retrieve an asset by its ID.
 
    Path examples:
      GET /assets/wildlife/hawk.png
      GET /assets/wildlife/hawk          (extension auto-detected)
      GET /assets/game_sprites/player_idle.png
 
    Response headers:
      X-Cache: HIT  — served from in-memory cache
      X-Cache: MISS — loaded from disk and added to cache
    """
    #retrience an asset by its id
    #ex: GET /assets/wildlife/hawk.png
    #response headers: X-Cache: HIT  — served from in-memory cache
                      #X-Cache: MISS — loaded from disk and added to cache

    #check in mem cach first
    cached = _cache_get(asset_id)
    if cached:
        data, mimetype = cached
        response = app.response_class(data, mimetype=mimetype, status=200)
        response.headers["X-Cache"] = "HIT"
        return response
 
    #resolve path and load from disk 
    try:
        base_path = _asset_path(asset_id)
    except ValueError:
        return jsonify({"error": "Invalid asset_id", "asset_id": asset_id}), 400
 
    file_path = _find_asset_file(base_path)
    if file_path is None:
        return jsonify({"error": "Asset not found", "asset_id": asset_id}), 404
 
    data = file_path.read_bytes()
    mimetype, _ = mimetypes.guess_type(str(file_path))
    mimetype = mimetype or "application/octet-stream"
 
    #store in cache for next request 
    _cache_set(asset_id, data, mimetype)
 
    response = app.response_class(data, mimetype=mimetype, status=200)
    response.headers["X-Cache"] = "MISS"
    return response
 
 
@app.route("/assets/<path:asset_id>", methods=["POST"])
def upload_asset(asset_id: str):
   
    #upload or replace asset, invalidate existing cache entry
    #gets a multipart/form-data with a 'file' field
    #returns a 201 on success
    if "file" not in request.files:
        return jsonify({"error": "No file field in request", "asset_id": asset_id}), 400
 
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename", "asset_id": asset_id}), 400
 
    # size guard
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_ASSET_SIZE_BYTES:
        return jsonify({"error": "File too large (max 10 MB)", "asset_id": asset_id}), 413
 
    try:
        dest_path = _asset_path(asset_id)
    except ValueError:
        return jsonify({"error": "Invalid asset_id", "asset_id": asset_id}), 400
 
    # create parent directories if needed
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(dest_path))
 
    # invalidate cache so the next GET fetches the new version from disk
    _cache_invalidate(asset_id)
 
    return jsonify({
        "message": "Asset uploaded successfully",
        "asset_id": asset_id,
        "size_bytes": size,
    }), 201
 
 
# entry point
 
if __name__ == "__main__":
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[media-service] Assets directory: {ASSETS_DIR}")
    print(f"[media-service] Cache TTL: {CACHE_TTL_SECONDS}s")
    app.run(host="0.0.0.0", port=5200, debug=False)
