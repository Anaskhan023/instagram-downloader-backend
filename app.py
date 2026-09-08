from flask import Flask, request, jsonify, Response, send_file
import yt_dlp
import os
import tempfile
import glob
import shutil
import subprocess
import time
import hmac
import hashlib
import logging
from urllib.parse import urlparse

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================

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

BACKEND_API_SECRET = os.environ.get(
    "BACKEND_API_SECRET",
    ""
)

RATE_WINDOW = 60
MAX_REQUESTS_PER_WINDOW = 12
DOWNLOAD_TOKEN_TTL = 180

request_log = {}


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# SECURITY HELPERS
# =========================================================

def get_client_ip():
    return request.remote_addr or "unknown"


def cleanup_rate_log():

    now = time.time()

    expired = [
        ip
        for ip, timestamps in request_log.items()
        if not timestamps
        or timestamps[-1] < now - RATE_WINDOW
    ]

    for ip in expired:
        request_log.pop(ip, None)


def rate_limit_ok():

    cleanup_rate_log()

    ip = get_client_ip()
    now = time.time()

    timestamps = request_log.setdefault(
        ip,
        []
    )

    timestamps[:] = [
        ts
        for ts in timestamps
        if ts > now - RATE_WINDOW
    ]

    if len(timestamps) >= MAX_REQUESTS_PER_WINDOW:
        return False

    timestamps.append(now)

    return True


def is_authorized_server_request():

    if not BACKEND_API_SECRET:
        logger.error(
            "BACKEND_API_SECRET is missing."
        )
        return False

    supplied_secret = request.headers.get(
        "X-Backend-Secret",
        ""
    )

    return hmac.compare_digest(
        supplied_secret,
        BACKEND_API_SECRET
    )


def unauthorized():

    return jsonify({
        "success": False,
        "message": "Unauthorized request."
    }), 403


def too_many_requests():

    return jsonify({
        "success": False,
        "message": (
            "Too many requests. "
            "Please wait a moment."
        )
    }), 429


# =========================================================
# INSTAGRAM URL VALIDATION
# =========================================================

def is_valid_instagram_url(url):

    try:

        parsed = urlparse(url)

        host = (
            parsed.hostname
            or ""
        ).lower()

        path = (
            parsed.path
            or ""
        )

        return (
            parsed.scheme in {
                "http",
                "https"
            }
            and host in ALLOWED_HOSTS
            and (
                path.startswith("/p/")
                or path.startswith("/reel/")
                or path.startswith("/tv/")
            )
        )

    except Exception:
        return False


# =========================================================
# YT-DLP INFO
# =========================================================

def get_ydl_info(url):

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,

        "http_headers": COMMON_HEADERS,

        "socket_timeout": 20,
    }

    with yt_dlp.YoutubeDL(
        ydl_opts
    ) as ydl:

        return ydl.extract_info(
            url,
            download=False
        )


# =========================================================
# AUDIO FORMAT SELECTION
# =========================================================

def choose_audio_format(info):

    formats = (
        info.get("formats")
        or []
    )

    candidates = []

    for fmt in formats:

        media_url = fmt.get("url")
        acodec = fmt.get("acodec")

        if (
            media_url
            and acodec
            and acodec != "none"
        ):
            candidates.append(fmt)

    if not candidates:
        return None

    def audio_score(fmt):

        abr = float(
            fmt.get("abr")
            or 0
        )

        tbr = float(
            fmt.get("tbr")
            or 0
        )

        asr = int(
            fmt.get("asr")
            or 0
        )

        return (
            abr,
            tbr,
            asr
        )

    candidates.sort(
        key=audio_score,
        reverse=True
    )

    return candidates[0]


# =========================================================
# SIGNATURE VERIFICATION
# =========================================================

def verify_download_signature(
    url,
    file_format,
    expires,
    signature
):

    if not BACKEND_API_SECRET:
        return False

    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        return False

    now = int(time.time())

    if expires_int < now:
        return False

    if expires_int > now + DOWNLOAD_TOKEN_TTL + 30:
        return False

    message = (
        url
        + "|"
        + file_format
        + "|"
        + str(expires_int)
    )

    expected_signature = hmac.new(
        BACKEND_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        signature or "",
        expected_signature
    )


# =========================================================
# SECURITY HEADERS
# =========================================================

@app.after_request
def add_security_headers(response):

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    response.headers[
        "X-Frame-Options"
    ] = "DENY"

    response.headers[
        "Referrer-Policy"
    ] = "strict-origin-when-cross-origin"

    response.headers[
        "Cache-Control"
    ] = "no-store"

    response.headers[
        "Access-Control-Allow-Origin"
    ] = "https://badshashoes.store"

    response.headers[
        "Access-Control-Allow-Methods"
    ] = "GET, POST, OPTIONS"

    response.headers[
        "Access-Control-Allow-Headers"
    ] = (
        "Content-Type, X-Backend-Secret"
    )

    return response


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({
        "status": "success",
        "message": (
            "Instagram Downloader Backend is running."
        )
    })


# =========================================================
# RESOLVE ENDPOINT
# =========================================================

@app.route(
    "/api/resolve",
    methods=[
        "GET",
        "POST",
        "OPTIONS"
    ]
)
def resolve_instagram():

    if request.method == "OPTIONS":
        return Response(
            status=204
        )

    if not is_authorized_server_request():

        logger.warning(
            "Unauthorized resolve request from %s",
            get_client_ip()
        )

        return unauthorized()

    if not rate_limit_ok():
        return too_many_requests()

    if request.method == "POST":

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

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
            "message": (
                "Please enter an Instagram URL."
            )
        }), 400

    if len(url) > 1000:

        return jsonify({
            "success": False,
            "message": (
                "URL is too long."
            )
        }), 400

    if not is_valid_instagram_url(url):

        return jsonify({
            "success": False,
            "message": (
                "Please enter a valid public "
                "Instagram post or Reel URL."
            )
        }), 400

    try:

        logger.info(
            "Resolving Instagram URL."
        )

        info = get_ydl_info(
            url
        )

        media_url = info.get(
            "url"
        )

        if not media_url:

            formats = (
                info.get("formats")
                or []
            )

            usable = [
                f
                for f in formats
                if (
                    f.get("url")
                    and (
                        f.get("vcodec") != "none"
                        or f.get("acodec") != "none"
                    )
                )
            ]

            usable.sort(
                key=lambda f: float(
                    f.get("tbr")
                    or 0
                ),
                reverse=True
            )

            if usable:
                media_url = usable[0].get(
                    "url"
                )

        if not media_url:

            logger.warning(
                "No downloadable media found."
            )

            return jsonify({
                "success": False,
                "message": (
                    "No downloadable media was found."
                )
            }), 404

        return jsonify({
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "media_url": media_url,
            "message": (
                "Instagram media resolved successfully."
            )
        })

    except Exception:

        logger.exception(
            "Instagram resolve failed."
        )

        return jsonify({
            "success": False,
            "message": (
                "This public media could not be processed."
            )
        }), 400


# =========================================================
# DOWNLOAD ENDPOINT
# =========================================================

@app.route(
    "/api/download",
    methods=["GET"]
)
def download_instagram():

    logger.info(
        "Download request received."
    )

    # -----------------------------------------------------
    # SECRET
    # -----------------------------------------------------

    if not is_authorized_server_request():

        logger.warning(
            "Unauthorized download request."
        )

        return unauthorized()


    # -----------------------------------------------------
    # RATE LIMIT
    # -----------------------------------------------------

    if not rate_limit_ok():

        logger.warning(
            "Download rate limit exceeded."
        )

        return too_many_requests()


    # -----------------------------------------------------
    # PARAMETERS
    # -----------------------------------------------------

    url = (
        request.args.get("url")
        or ""
    ).strip()

    file_format = (
        request.args.get("format")
        or "mp3"
    ).lower()

    expires = (
        request.args.get("expires")
        or ""
    )

    signature = (
        request.args.get("signature")
        or ""
    )


    # -----------------------------------------------------
    # BASIC VALIDATION
    # -----------------------------------------------------

    if not url:

        return jsonify({
            "success": False,
            "message": "Missing URL."
        }), 400


    if not is_valid_instagram_url(url):

        logger.warning(
            "Invalid Instagram URL in download."
        )

        return jsonify({
            "success": False,
            "message": "Invalid Instagram URL."
        }), 400


    if file_format not in {
        "mp3",
        "mp4"
    }:

        return jsonify({
            "success": False,
            "message": "Unsupported format."
        }), 400


    # -----------------------------------------------------
    # SIGNATURE
    # -----------------------------------------------------

    if not verify_download_signature(
        url,
        file_format,
        expires,
        signature
    ):

        logger.warning(
            "Invalid or expired download signature."
        )

        return jsonify({
            "success": False,
            "message": (
                "Download link is invalid or expired."
            )
        }), 403


    # -----------------------------------------------------
    # FFMPEG CHECK
    # -----------------------------------------------------

    ffmpeg_path = shutil.which(
        "ffmpeg"
    )

    if not ffmpeg_path:

        logger.error(
            "FFmpeg was not found."
        )

        return jsonify({
            "success": False,
            "message": (
                "Media conversion is temporarily unavailable."
            )
        }), 500


    logger.info(
        "FFmpeg found at %s",
        ffmpeg_path
    )


    # -----------------------------------------------------
    # TEMP DIRECTORY
    # -----------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="instagram_"
    )


    try:

        # =================================================
        # MP4
        # =================================================

        if file_format == "mp4":

            logger.info(
                "Starting MP4 download."
            )

            output_template = os.path.join(
                temp_dir,
                "instagram-video.%(ext)s"
            )

            ydl_opts = {

                "quiet": False,

                "no_warnings": False,

                "noplaylist": True,

                "format": "bv*+ba/b",

                "merge_output_format": "mp4",

                "outtmpl": output_template,

                "http_headers":
                    COMMON_HEADERS,

                "socket_timeout": 20,

                "max_filesize":
                    100 * 1024 * 1024,
            }


            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                ydl.download([
                    url
                ])


            mp4_files = glob.glob(
                os.path.join(
                    temp_dir,
                    "*.mp4"
                )
            )


            logger.info(
                "MP4 files created: %s",
                len(mp4_files)
            )


            if not mp4_files:

                raise RuntimeError(
                    "MP4 file was not created."
                )


            response = send_file(
                mp4_files[0],
                mimetype="video/mp4",
                as_attachment=True,
                download_name=(
                    "instagram-video.mp4"
                )
            )


        # =================================================
        # MP3
        # =================================================

        else:

            logger.info(
                "Starting MP3 processing."
            )

            info = get_ydl_info(
                url
            )

            audio_format = (
                choose_audio_format(
                    info
                )
            )


            if not audio_format:

                raise RuntimeError(
                    "No audio-capable format was found."
                )


            format_id = (
                audio_format.get(
                    "format_id"
                )
            )


            audio_codec = (
                audio_format.get(
                    "acodec"
                )
            )


            logger.info(
                "Selected audio format: %s | codec: %s",
                format_id,
                audio_codec
            )


            if not format_id:

                raise RuntimeError(
                    "Audio format ID was unavailable."
                )


            source_template = os.path.join(
                temp_dir,
                "instagram-source.%(ext)s"
            )


            ydl_opts = {

                "quiet": False,

                "no_warnings": False,

                "noplaylist": True,

                "format": str(
                    format_id
                ),

                "outtmpl":
                    source_template,

                "http_headers":
                    COMMON_HEADERS,

                "socket_timeout": 20,

                "max_filesize":
                    100 * 1024 * 1024,
            }


            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                ydl.download([
                    url
                ])


            source_files = [

                f
                for f in glob.glob(
                    os.path.join(
                        temp_dir,
                        "*"
                    )
                )

                if (
                    os.path.isfile(f)
                    and not f.endswith(".part")
                    and not f.endswith(".mp3")
                )

            ]


            logger.info(
                "Source files created: %s",
                len(source_files)
            )


            if not source_files:

                raise RuntimeError(
                    "Audio source was not downloaded."
                )


            source_file = (
                source_files[0]
            )


            mp3_file = os.path.join(
                temp_dir,
                "instagram-audio.mp3"
            )


            logger.info(
                "Running FFmpeg conversion."
            )


            ffmpeg_command = [

                ffmpeg_path,

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

                mp3_file
            ]


            process = subprocess.run(

                ffmpeg_command,

                stdout=subprocess.PIPE,

                stderr=subprocess.PIPE,

                text=True,

                timeout=120
            )


            if process.returncode != 0:

                logger.error(
                    "FFmpeg failed:\n%s",
                    process.stderr[-4000:]
                )

                raise RuntimeError(
                    "FFmpeg audio conversion failed."
                )


            if not os.path.exists(
                mp3_file
            ):

                raise RuntimeError(
                    "MP3 file was not created."
                )


            logger.info(
                "MP3 created successfully."
            )


            response = send_file(
                mp3_file,
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name=(
                    "instagram-audio.mp3"
                )
            )


        # -------------------------------------------------
        # CLEANUP
        # -------------------------------------------------

        @response.call_on_close
        def cleanup():

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

        return response


    # -----------------------------------------------------
    # TIMEOUT
    # -----------------------------------------------------

    except subprocess.TimeoutExpired:

        logger.exception(
            "Download process timed out."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": (
                "Media processing took too long."
            )
        }), 500


    # -----------------------------------------------------
    # ALL OTHER ERRORS
    # -----------------------------------------------------

    except Exception:

        logger.exception(
            "Instagram download failed."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": (
                "Download failed. "
                "The media may be unavailable "
                "or temporarily unsupported."
            )
        }), 400


# =========================================================
# START SERVER
# =========================================================

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
