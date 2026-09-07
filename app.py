from flask import Flask, request, jsonify
import yt_dlp
from urllib.parse import urlparse

app = Flask(__name__)


def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "instagram.com",
            "www.instagram.com",
        }

        return (
            parsed.scheme in {"http", "https"}
            and host in allowed_hosts
            and parsed.path.startswith(("/p/", "/reel/", "/tv/"))
        )

    except Exception:
        return False


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "success",
        "message": "Instagram Downloader Backend is running."
    })


@app.route("/api/resolve", methods=["POST"])
def resolve_instagram():

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

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
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        return jsonify({
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "url": info.get("url"),
            "duration": info.get("duration"),
            "message": "Instagram media resolved successfully."
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": "Could not resolve this Instagram media.",
            "error": str(e)
        }), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
