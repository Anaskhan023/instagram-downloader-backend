from flask import Flask, request, jsonify, Response, stream_with_context, send_file
import yt_dlp
import requests
import os
import tempfile
import glob
import shutil
import subprocess
from urllib.parse import urlparse

app = Flask(__name__)

ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}

COMMON_HEADERS = {
    "Referer": "https://www.instagram.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
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
        "http_headers": COMMON_HEADERS,
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
    file_format = (request.args.get("format") or "mp3").lower()

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

    if file_format not in {"mp3", "mp4"}:
        return jsonify({
            "success": False,
            "message": "Unsupported format."
        }), 400

    if not shutil.which("ffmpeg"):
        return jsonify({
            "success": False,
            "message": "FFmpeg is not available on the server."
        }), 500

    temp_dir = tempfile.mkdtemp(prefix="instagram_")

    try:

        # -------------------------
        # MP4
        # -------------------------
        if file_format == "mp4":

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "best[acodec!=none]/best",
                "outtmpl": os.path.join(
                    temp_dir,
                    "instagram_video.%(ext)s"
                ),
                "http_headers": COMMON_HEADERS,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            video_files = [
                f for f in glob.glob(
                    os.path.join(temp_dir, "*")
                )
                if not f.lower().endswith(".part")
            ]

            if not video_files:
                raise RuntimeError("Video file was not created.")

            video_file = video_files[0]

            response = send_file(
                video_file,
                mimetype="video/mp4",
                as_attachment=True,
                download_name="instagram-video.mp4"
            )

        # -------------------------
        # MP3
        # -------------------------
        else:

            audio_input = os.path.join(
                temp_dir,
                "instagram_audio.%(ext)s"
            )

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "best[acodec!=none]/bestaudio/best",
                "outtmpl": audio_input,
                "http_headers": COMMON_HEADERS,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            downloaded_files = [
                f for f in glob.glob(
                    os.path.join(temp_dir, "*")
                )
                if (
                    os.path.isfile(f)
                    and not f.endswith(".part")
                    and not f.endswith(".mp3")
                )
            ]

            if not downloaded_files:
                raise RuntimeError(
                    "Audio-capable media was not downloaded."
                )

            source_file = downloaded_files[0]

            mp3_file = os.path.join(
                temp_dir,
                "instagram-audio.mp3"
            )

            ffmpeg_command = [
                "ffmpeg",
                "-y",
                "-i",
                source_file,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-b:a",
                "192k",
                mp3_file,
            ]

            process = subprocess.run(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )

            if process.returncode != 0:
                raise RuntimeError(
                    process.stderr[-3000:]
                )

            if not os.path.exists(mp3_file):
                raise RuntimeError(
                    "MP3 file was not created."
                )

            response = send_file(
                mp3_file,
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name="instagram-audio.mp3"
            )

        @response.call_on_close
        def cleanup():
            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

        return response

    except subprocess.TimeoutExpired:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": "MP3 conversion timed out."
        }), 500

    except Exception as e:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": "Download failed.",
            "error": str(e)
        }), 400


if __name__ == "__main__":
    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
