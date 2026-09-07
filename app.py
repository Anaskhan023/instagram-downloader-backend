from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests
import os
from urllib.parse import urlparse

app = Flask(__name__)

ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}


def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in {"http", "https"}
            and host in ALLOWED_HOSTS
            and parsed.path.startswith(("/p/", "/reel/", "/tv/"))
        )

    except Exception:
        return False


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "success",
        "message": "Instagram Downloader Backend is running."
    })


@app.route("/api/resolve", methods=["GET", "POST", "OPTIONS"])
def resolve_instagram():

    if request.method == "OPTIONS":
        return Response(status=204)

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or request.form.get("url") or "").strip()
    else:
        url = (request.args.get("url") or "").strip()

    if not url:
        return jsonify({
            "success": False,
            "message": "Please enter an Instagram URL."
        }), 400

    if not is_valid_instagram_url(url):
        return jsonify({
            "success": False,
            "message": "Please enter a valid public Instagram post or Reel URL."
        }), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": {
            "Referer": "https://www.instagram.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        media_url = info.get("url")

        if not media_url:
            return jsonify({
                "success": False,
                "message": "No downloadable media was found."
            }), 404

        return jsonify({
            "success": True,
            "type": "video",
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "media_url": media_url,
            "message": "Instagram media resolved successfully."
        })

    except Exception:
        return jsonify({
            "success": False,
            "message": (
                "Could not resolve this Instagram media. "
                "The post may be private, unavailable, or temporarily unsupported."
            )
        }), 400


@app.route("/api/download", methods=["GET"])
def download_instagram():

    url = (request.args.get("url") or "").strip()

    if not url:
        return jsonify({
            "success": False,
            "message": "Please enter an Instagram URL."
        }), 400

    if not is_valid_instagram_url(url):
        return jsonify({
            "success": False,
            "message": "Invalid Instagram URL."
        }), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": {
            "Referer": "https://www.instagram.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        media_url = info.get("url")

        if not media_url:
            return jsonify({
                "success": False,
                "message": "No downloadable media found."
            }), 404

        media_headers = info.get("http_headers", {})

        upstream = requests.get(
            media_url,
            headers=media_headers,
            stream=True,
            timeout=60
        )

        upstream.raise_for_status()

        content_type = (
            upstream.headers.get("Content-Type")
            or "application/octet-stream"
        )

        filename = "instagram-media.mp4"

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        response = Response(
            stream_with_context(generate()),
            content_type=content_type
        )

        response.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )

        return response

    except Exception:
        return jsonify({
            "success": False,
            "message": "Media download failed. Please try again."
        }), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
