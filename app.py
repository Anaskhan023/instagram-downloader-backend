from flask import Flask, request, jsonify, Response, send_file
import yt_dlp
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
        path = parsed.path or ""

        return (
            parsed.scheme in {"http", "https"}
            and host in ALLOWED_HOSTS
            and (
                path.startswith("/p/")
                or path.startswith("/reel/")
                or path.startswith("/tv/")
            )
        )

    except Exception:
        return False


def get_ydl_info(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": COMMON_HEADERS,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def choose_audio_format(info):
    formats = info.get("formats") or []

    audio_formats = []

    for fmt in formats:
        acodec = fmt.get("acodec")
        media_url = fmt.get("url")

        if (
            media_url
            and acodec
            and acodec != "none"
        ):
            audio_formats.append(fmt)

    if not audio_formats:
        return None

    def audio_score(fmt):
        abr = fmt.get("abr")
        tbr = fmt.get("tbr")
        asr = fmt.get("asr")

        return (
            float(abr or 0),
            float(tbr or 0),
            int(asr or 0),
        )

    audio_formats.sort(
        key=audio_score,
        reverse=True
    )

    return audio_formats[0]


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
        url = (
            data.get("url")
            or request.form.get("url")
            or ""
        ).strip()
    else:
        url = (
            request.args.get("url")
            or ""
        ).strip()

    if not url:
        return jsonify({
            "success": False,
            "message": "Please enter an Instagram URL."
        }), 400

    if not is_valid_instagram_url(url):
        return jsonify({
            "success": False,
            "message": (
                "Please enter a valid public Instagram "
                "post or Reel URL."
            )
        }), 400

    try:
        info = get_ydl_info(url)

        formats = info.get("formats") or []

        media_url = info.get("url")

        # If the main URL is missing, find a usable format.
        if not media_url:
            usable = [
                f for f in formats
                if f.get("url")
                and (
                    f.get("vcodec") != "none"
                    or f.get("acodec") != "none"
                )
            ]

            if usable:
                usable.sort(
                    key=lambda f: float(f.get("tbr") or 0),
                    reverse=True
                )
                media_url = usable[0].get("url")

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
                "The post may be private, unavailable, "
                "or temporarily unsupported."
            )
        }), 400


@app.route("/api/download", methods=["GET"])
def download_instagram():

    url = (
        request.args.get("url")
        or ""
    ).strip()

    file_format = (
        request.args.get("format")
        or "mp3"
    ).lower()

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

    temp_dir = tempfile.mkdtemp(
        prefix="instagram_"
    )

    try:

        # =====================================
        # MP4
        # =====================================

        if file_format == "mp4":

            output_template = os.path.join(
                temp_dir,
                "instagram-video.%(ext)s"
            )

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,

                # Best video+audio where possible,
                # with combined-format fallback.
                "format": "bv*+ba/b",

                "merge_output_format": "mp4",

                "outtmpl": output_template,

                "http_headers": COMMON_HEADERS,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            mp4_files = glob.glob(
                os.path.join(
                    temp_dir,
                    "*.mp4"
                )
            )

            if not mp4_files:
                raise RuntimeError(
                    "MP4 file was not created."
                )

            mp4_file = mp4_files[0]

            response = send_file(
                mp4_file,
                mimetype="video/mp4",
                as_attachment=True,
                download_name="instagram-video.mp4"
            )

        # =====================================
        # MP3
        # =====================================

        else:

            # First inspect the available formats.
            info = get_ydl_info(url)

            audio_format = choose_audio_format(info)

            if not audio_format:
                raise RuntimeError(
                    "No audio-capable Instagram format was found."
                )

            format_id = audio_format.get(
                "format_id"
            )

            if not format_id:
                raise RuntimeError(
                    "Audio format ID was not available."
                )

            source_template = os.path.join(
                temp_dir,
                "instagram-source.%(ext)s"
            )

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": str(format_id),
                "outtmpl": source_template,
                "http_headers": COMMON_HEADERS,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            source_files = [
                f for f in glob.glob(
                    os.path.join(temp_dir, "*")
                )
                if (
                    os.path.isfile(f)
                    and not f.endswith(".part")
                )
            ]

            if not source_files:
                raise RuntimeError(
                    "Audio source was not downloaded."
                )

            source_file = source_files[0]

            mp3_file = os.path.join(
                temp_dir,
                "instagram-audio.mp3"
            )

            # Extract audio directly with FFmpeg.
            ffmpeg_command = [
                "ffmpeg",
                "-y",
                "-i",
                source_file,
                "-map",
                "0:a:0",
                "-vn",
                "-codec:a",
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
                    process.stderr[-4000:]
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
            "message": "Audio conversion timed out."
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
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
