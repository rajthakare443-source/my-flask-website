from werkzeug.security import generate_password_hash, check_password_hash
import yt_dlp
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, send_file, flash
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import instaloader
from instaloader import Instaloader, Post, exceptions, Profile
import yt_dlp
import sqlite3
import requests
import urllib.parse
from datetime import datetime
import logging
import subprocess
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"
UPLOAD_FOLDER = BASE_DIR / "user_photos"
DOWNLOAD_FOLDER = BASE_DIR / "downloads"
DOWNLOAD_BASE = BASE_DIR / "downloads"  # fallback for legacy code, can be same as DOWNLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
UPLOAD_FOLDER.mkdir(exist_ok=True)
DOWNLOAD_FOLDER.mkdir(exist_ok=True)

# configure simple logging for better diagnostics
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    # Ensure base table exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            url TEXT
        )
    """)
    # Ensure additional columns exist (add if missing)
    c.execute("PRAGMA table_info(history)")
    cols = [row[1] for row in c.fetchall()]
    if "filename" not in cols:
        c.execute("ALTER TABLE history ADD COLUMN filename TEXT")
    if "downloaded_at" not in cols:
        c.execute("ALTER TABLE history ADD COLUMN downloaded_at TEXT")
    # Add users table for authentication
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT,
            photo TEXT
        )
    """)
    # Add photo column if missing
    c.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in c.fetchall()]
    if "photo" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN photo TEXT")
    conn.commit()
    conn.close()

def add_history(platform, url, filename=None):
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO history (platform, url, filename, downloaded_at) VALUES (?, ?, ?, ?)",
        (platform, url, filename, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

init_db()

def _extract_shortcode_from_url(url):
    """Return shortcode from Instagram URL or raw shortcode, otherwise None."""
    if not url:
        return None
    url = url.strip()
    # Raw shortcode input (common)
    if re.fullmatch(r"[A-Za-z0-9_-]{5,}", url):
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return None
        # Common patterns: /p/SHORTCODE/, /reel/SHORTCODE/, /tv/SHORTCODE/
        for i, part in enumerate(parts):
            if part in ("p", "reel", "tv") and i + 1 < len(parts):
                return parts[i + 1]
        # Fallback: last path segment if it looks like a shortcode
        candidate = parts[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{5,}", candidate):
            return candidate
        return None
    except Exception:
        return None

def _try_load_instaloader_session(L):
    """Attempt to load session file; return True if loaded, False otherwise."""
    try:
        if os.path.exists(INSTALOADER_SESSION_PATH):
            L.load_session_from_file(INSTAGRAM_USERNAME, filename=INSTALOADER_SESSION_PATH)
            print("Loaded instaloader session from configured path.")
            return True
        # Try default session path
        L.load_session_from_file(INSTAGRAM_USERNAME)
        print("Loaded instaloader session from default path.")
        return True
    except FileNotFoundError:
        print("Instaloader session file not found; proceeding anonymously.")
        return False
    except Exception as e:
        print("Failed to load instaloader session:", e)
        return False

def _save_url_to_file(url, out_path):
    """Stream a URL to a file with browser headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, stream=True, headers=headers, timeout=30)
    resp.raise_for_status()
    with open(out_path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)
    return out_path

def _download_post_media(post, shortcode, target_dir):
    """Download media for a Post instance, return list of saved file paths."""
    os.makedirs(target_dir, exist_ok=True)
    downloaded = []

    # Try sidecar nodes (multiple media)
    try:
        nodes = list(post.get_sidecar_nodes())
    except Exception:
        nodes = []

    if nodes:
        for idx, node in enumerate(nodes, start=1):
            if getattr(node, "is_video", False):
                media_url = getattr(node, "video_url", None)
                if media_url:
                    ext = os.path.splitext(urllib.parse.urlparse(media_url).path)[1] or ".mp4"
                    out = os.path.join(target_dir, f"{shortcode}_{idx}{ext}")
                    _save_url_to_file(media_url, out)
                    downloaded.append(out)
            else:
                media_url = getattr(node, "display_url", None) or getattr(node, "thumbnail_url", None)
                if media_url:
                    ext = os.path.splitext(urllib.parse.urlparse(media_url).path)[1] or ".jpg"
                    out = os.path.join(target_dir, f"{shortcode}_{idx}{ext}")
                    _save_url_to_file(media_url, out)
                    downloaded.append(out)
    else:
        # Single video
        if getattr(post, "is_video", False):
            media_url = getattr(post, "video_url", None)
            if media_url:
                ext = os.path.splitext(urllib.parse.urlparse(media_url).path)[1] or ".mp4"
                out = os.path.join(target_dir, f"{shortcode}{ext}")
                _save_url_to_file(media_url, out)
                downloaded.append(out)
        # Single image
        else:
            media_url = getattr(post, "url", None) or getattr(post, "display_url", None)
            if media_url:
                ext = os.path.splitext(urllib.parse.urlparse(media_url).path)[1] or ".jpg"
                out = os.path.join(target_dir, f"{shortcode}{ext}")
                _save_url_to_file(media_url, out)
                downloaded.append(out)

    return downloaded

def extract_shortcode(url: str) -> str | None:
    m = re.search(r"(?:/p/|/reel/|/reels/|/tv/)([^/?#&]+)", url or "")
    return m.group(1) if m else None

def try_get_post(shortcode: str, loader: Instaloader):
    try:
        return Post.from_shortcode(loader.context, shortcode)
    except exceptions.QueryReturnedNotFoundException:
        raise
    except exceptions.LoginRequiredException as e:
        raise
    except exceptions.InstaloaderException:
        # Generic failure
        raise

def download_post_to_temp(loader: Instaloader, post: Post):
    """Download a post into a temp dir and return (tmpdir_path, list_of_media_paths).
       If Instaloader didn't write any media files, try the direct-media fallback (_download_post_media)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="insta_dl_"))
    try:
        # Attempt normal instaloader download first
        try:
            loader.download_post(post, target=str(tmpdir))
        except Exception:
            # ignore and try fallback later
            pass

        media_exts = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".webm"}
        media_files = [p for p in tmpdir.rglob("*") if p.is_file() and p.suffix.lower() in media_exts]

        # If no media saved by Instaloader, use our URL-based downloader into same tmpdir
        if not media_files:
            shortcode = getattr(post, "shortcode", None) or "post"
            downloaded = _download_post_media(post, shortcode, str(tmpdir))
            media_files = [Path(p) for p in downloaded]

        # final check: if still nothing, return empty list (caller handles message)
        return tmpdir, sorted(media_files)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

def download_profile_to_temp(loader: Instaloader, profile: Profile, max_posts: int | None = None):
    """Download profile posts into a temp dir; return (tmpdir, list[Path])."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"profile_{profile.username}_"))
    try:
        count = 0
        for post in profile.get_posts():
            try:
                loader.download_post(post, target=str(tmpdir))
            except Exception:
                # fallback to URL-based download per post
                _download_post_media(post, getattr(post, "shortcode", f"post{count}"), str(tmpdir))
            count += 1
            if max_posts and count >= max_posts:
                break
        media_exts = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".webm"}
        media_files = [p for p in tmpdir.rglob("*") if p.is_file() and p.suffix.lower() in media_exts]
        return tmpdir, sorted(media_files)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

def _schedule_cleanup(paths_to_remove, delay=5):
    def _clean():
        time.sleep(delay)
        for p in paths_to_remove:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
            except Exception:
                pass
    t = threading.Thread(target=_clean, daemon=True)
    t.start()

def send_download_response(media_paths, tmpdir: Path, basename: str = "instagram_post"):
    """(legacy) kept for backward compatibility — use persist_media_and_send instead."""
    if not media_paths:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("No files downloaded for this post.")

    # If only one file, send it directly
    if len(media_paths) == 1:
        file_path = media_paths[0]
        _schedule_cleanup([tmpdir])
        return send_file(
            str(file_path),
            as_attachment=True,
            download_name=file_path.name
        )

    # Multiple files
    saved_files = []
    # platform and source_url are not defined in this legacy function, so skip add_history here
    for p in media_paths:
        dest = DOWNLOAD_FOLDER / p.name
        shutil.copy2(p, dest)
        saved_files.append(dest.name)
        # add_history(platform, source_url or "", p.name)  # Not available here

    _schedule_cleanup([tmpdir], delay=10)

    # single file after copy
    if len(saved_files) == 1:
        return send_file(
            DOWNLOAD_FOLDER / saved_files[0],
            as_attachment=True
        )

    # multiple files (NO ZIP)
    return render_template(
        "multi_download.html",
        files=saved_files
    )

def persist_media_and_send(media_paths, tmpdir: Path, basename: str = "instagram_post", platform: str = "instagram", source_url: str | None = None):
    """Persist downloaded media (single file or multi-file) into DOWNLOAD_FOLDER, record history, and send file(s)."""
    if not media_paths:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("No files downloaded for this post.")

    timestamp = int(time.time())
    # Single file: send directly
    if len(media_paths) == 1:
        src = media_paths[0]
        dest_name = f"{timestamp}_{src.name}"
        dest = DOWNLOAD_FOLDER / dest_name
        shutil.copy2(src, dest)
        add_history(platform, source_url or "", dest_name)
        _schedule_cleanup([tmpdir], delay=10)
        return send_file(str(dest), as_attachment=True, download_name=src.name)

    # Multiple files: copy each, record, and show download links (no zip)
    saved_files = []
    for src in media_paths:
        dest_name = f"{timestamp}_{src.name}"
        dest = DOWNLOAD_FOLDER / dest_name
        shutil.copy2(src, dest)
        saved_files.append(dest_name)
        add_history(platform, source_url or "", dest_name)
    _schedule_cleanup([tmpdir], delay=10)
    # Render multi_download.html with download links
    return render_template("multi_download.html", files=saved_files)

def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def _collapse_image_variants(media_paths: list[Path]) -> list[Path]:
    """
    If all files are images and one file is dominant (largest file >= 65% of total size),
    return that single file to avoid wrapping a single-image post into a zip.
    """
    if not media_paths or len(media_paths) == 1:
        return media_paths
    if not all(_is_image_file(p) for p in media_paths):
        return media_paths
    try:
        sizes = [(p, p.stat().st_size) for p in media_paths]
    except Exception:
        return media_paths
    total = sum(s for _, s in sizes)
    if total <= 0:
        return media_paths
    largest, largest_size = max(sizes, key=lambda x: x[1])
    if largest_size / total >= 0.65:
        return [largest]
    return media_paths

def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None

def _ffmpeg_extract_mp3(input_path: Path, out_path: Path):
    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(out_path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="ignore") or "ffmpeg failed")
    return out_path

def convert_media_paths_to_mp3(media_paths: list[Path], tmpdir: Path, basename: str):
    """Convert video files in media_paths to MP3 files in tmpdir; skip images."""
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg not found on server (required for MP3 extraction).")
    mp3s = []
    idx = 1
    video_exts = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".mpeg"}
    for p in media_paths:
        if p.suffix.lower() in video_exts:
            out = tmpdir / f"{basename}_{idx}.mp3"
            _ffmpeg_extract_mp3(p, out)
            mp3s.append(out)
            idx += 1
    if not mp3s:
        raise RuntimeError("No audio-capable media found for MP3 conversion.")
    return mp3s

def allowed_file(filename):
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

# ================= HOME =================
@app.route("/")
def index():
    user = None
    photo = None
    if "user_id" in session:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, photo FROM users WHERE id=?", (session["user_id"],))
        row = c.fetchone()
        conn.close()
        if row:
            user, photo = row
    return render_template("index.html", user=user, photo=photo)

# ================= INSTAGRAM =================
@app.route("/insta_login", methods=["GET", "POST"])
def insta_login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        url = request.form.get("url", "")
        requested_format = request.form.get("format") or request.args.get("format") or "original"
        shortcode = extract_shortcode(url)
        next_action = request.form.get("next", "") or request.args.get("next", "")
        if not username or not password:
            return render_template("insta_login.html", message="Username and password are required.", username=username, url=url, format=requested_format)

        L = Instaloader(save_metadata=False, download_comments=False, compress_json=False)
        try:
            # try to reuse session if exists
            session_file = SESSION_DIR / f"{username}.session"
            if session_file.exists():
                L.load_session_from_file(username, filename=str(session_file))
            else:
                L.login(username, password)
                L.save_session_to_file(filename=str(session_file))

            if shortcode:
                # Fetch metadata with clear handling
                try:
                    post = try_get_post(shortcode, L)
                except exceptions.QueryReturnedNotFoundException:
                    return render_template("insta_login.html", message="Post not found (may be private or deleted).", username=username, url=url, format=requested_format)
                except exceptions.InstaloaderException as e:
                    logger.exception("Instaloader failed to fetch post metadata after login")
                    return render_template("insta_login.html", message=f"Failed to fetch post metadata: {e}", username=username, url=url, format=requested_format)
                except Exception:
                    logger.exception("Unexpected error fetching post after login")
                    return render_template("insta_login.html", message="An unexpected error occurred while fetching post metadata.", username=username, url=url, format=requested_format)

                try:
                    tmpdir, media_files = download_post_to_temp(L, post)
                except Exception as e:
                    logger.exception("Failed to download media for post after login")
                    return render_template("insta_login.html", message=f"Failed to download media: {e}", username=username, url=url, format=requested_format)

                # collapse image variants to avoid single-image zips
                media_files = _collapse_image_variants(media_files)

                if not media_files:
                    return render_template("insta_login.html", message="No downloadable media found for this post (it may be removed or restricted).", username=username, url=url, format=requested_format)

                if requested_format == "mp3":
                    try:
                        mp3_files = convert_media_paths_to_mp3(media_files, tmpdir, basename=shortcode + "_audio")
                    except Exception as e:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        return render_template("insta_login.html", message=f"MP3 extraction failed: {e}", username=username, url=url, format=requested_format)
                    return persist_media_and_send(mp3_files, tmpdir, basename=f"{shortcode}_audio", platform="instagram_audio", source_url=url)

                return persist_media_and_send(media_files, tmpdir, basename=shortcode, platform="instagram_post", source_url=url)

               # ===== RESUME PROFILE DOWNLOAD AFTER LOGIN =====
            if next_action and next_action.startswith("profile:"):
                prof = next_action.split(":", 1)[1]

                try:
                    profile_obj = Profile.from_username(L.context, prof)
                except Exception as e:
                    return render_template(
                        "insta_login.html",
                        message="Could not fetch profile after login.",
                        details=str(e),
                        username=username,
                        url=url
                    )

                tmpdir, media_files = download_profile_to_temp(L, profile_obj)

                if not media_files:
                    return render_template(
                        "insta_login.html",
                        message="No downloadable media found for this profile.",
                        username=username,
                        url=url
                    )

                saved_files = []

                for p in media_files:
                    final_path = DOWNLOAD_BASE / p.name
                    shutil.move(str(p), final_path)
                    saved_files.append(final_path.name)

                add_history(
                    "instagram_profile",
                    f"https://instagram.com/{prof}/",
                    ", ".join(saved_files)
                )

                return render_template(
                    "profile_view.html",
                    profile_name=prof,
                    is_private=profile_obj.is_private,
                    files=saved_files
                )


            return redirect(url_for("instagram"))
        except Exception as e:
            return render_template("insta_login.html", message="Login or fetch failed.", details=str(e), username=username, url=url, format=requested_format)

    # GET request
    message = request.args.get("message")
    url = request.args.get("url", "")
    next_action = request.args.get("next", "")
    requested_format = request.args.get("format", "original")
    return render_template("insta_login.html", message=message, url=url, next=next_action, format=requested_format)

@app.route("/instagram", methods=["GET", "POST"])
def instagram():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        requested_format = request.form.get("format", "original")
        shortcode = extract_shortcode(url)
        if not shortcode:
            flash("Invalid URL format. Paste a post/reel URL (e.g. /p/SHORTCODE/)", "error")
            return render_template("insta_tools.html", url=url)
        L = Instaloader(save_metadata=False, download_comments=False, compress_json=False)

        # If user is logged in and post is private, try to use their website credentials for Instagram login
        try:
            post = try_get_post(shortcode, L)
        except exceptions.LoginRequiredException:
            if "user_id" in session:
                # Try to use the website username as Instagram username (or prompt for Instagram login)
                return redirect(url_for("insta_login", url=url, message="Post appears to be private. Please log in to Instagram to access it.", format=requested_format))
            else:
                return redirect(url_for("insta_login", url=url, message="Post appears to be private. Please log in to access it.", format=requested_format))
        except exceptions.QueryReturnedNotFoundException:
            flash("Post not found (may be private or deleted).", "error")
            return render_template("insta_tools.html", url=url)
        except exceptions.InstaloaderException as e:
            logger.exception("Instaloader failed to fetch post metadata")
            flash(f"Failed to fetch post metadata: {e}", "error")
            return render_template("insta_tools.html", url=url)
        except Exception as e:
            logger.exception("Unexpected error fetching post metadata")
            flash("An unexpected error occurred while fetching post metadata.", "error")
            return render_template("insta_tools.html", url=url)

        # Download media and handle download errors clearly
        try:
            tmpdir, media_files = download_post_to_temp(L, post)
        except Exception as e:
            logger.exception("Failed to download media for post")
            flash(f"Failed to download media for this post: {e}", "error")
            return render_template("insta_tools.html", url=url)

        # collapse image variants to avoid single-image zips
        media_files = _collapse_image_variants(media_files)

        if not media_files:
            flash("No downloadable media found for this post (it may be removed or restricted).", "error")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template("insta_tools.html", url=url)

        if requested_format == "mp3":
            try:
                mp3_files = convert_media_paths_to_mp3(media_files, tmpdir, basename=shortcode + "_audio")
            except Exception as e:
                shutil.rmtree(tmpdir, ignore_errors=True)
                flash(f"MP3 extraction failed: {e}", "error")
                return render_template("insta_tools.html", url=url)
            return persist_media_and_send(mp3_files, tmpdir, basename=f"{shortcode}_audio", platform="instagram_audio", source_url=url)

        return persist_media_and_send(media_files, tmpdir, basename=shortcode, platform="instagram_post", source_url=url)
    # GET
    return render_template("insta_tools.html")
    # Defensive: fallback in case of future code changes
    # return "Unexpected error", 500

# ================= YOUTUBE =================
@app.route("/youtube", methods=["GET", "POST"])
def youtube():
    if request.method == "POST":
        url = request.form.get("url")
        dtype = request.form.get("type")
        quality = request.form.get("quality", "best")  # expected: "best" or one of "4k","2160","1080","720","480","360","240"
 
        if not url or not dtype:
            return "URL or type missing"

        outtmpl = os.path.join(str(DOWNLOAD_FOLDER), "%(title).200s.%(ext)s")
        ydl_opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,

            # 🔑 CRITICAL FIXES
            "nocheckcertificate": True,
            "geo_bypass": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/122.0.0.0 Safari/537.36"
            }
        }

        if dtype == "mp3":
            flash("NO QUALITY REQUIRED for audio download.", "info")
            ydl_opts.update({
                 "format": "bestaudio",
                 "postprocessors": [{
                     "key": "FFmpegExtractAudio",
                     "preferredcodec": "mp3",
                     "preferredquality": "192",
                 }]
             })
        else:
            # Map requested quality to yt-dlp format selector
            quality_map = {
                "4k":   "bestvideo[height<=2160]+bestaudio/best[height<=2160]",  # alias for 2160p (4K)
                "2160": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
                "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
                "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
                "360":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
                "240":  "bestvideo[height<=240]+bestaudio/best[height<=240]",
            }
            if quality and quality in quality_map:
                fmt = quality_map[quality]
            else:
                # default: best mp4 or best available
                fmt = "best[ext=mp4]/best"
            ydl_opts.update({"format": fmt})
 
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if dtype == "mp3":
                    filename = filename.rsplit(".", 1)[0] + ".mp3"
            # Record download in history
            add_history("youtube", url, os.path.basename(filename))

        except Exception as e:
            return f"YouTube Error: {e}"

        return send_from_directory(
            str(DOWNLOAD_FOLDER),
            os.path.basename(filename),
            as_attachment=True
        )

    return render_template("youtube.html")

# ================= HISTORY =================
@app.route("/history")
def history():
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    c.execute("SELECT id, platform, url, filename, downloaded_at FROM history ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    history_items = []
    for r in rows:
        _id, platform, url, filename, downloaded_at = r
        filetype = None
        download_url = None
        if filename:
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".mp4", ".mov", ".mkv", ".webm"):
                filetype = "video"
            elif ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                filetype = "image"
            elif ext in (".mp3", ".m4a", ".wav", ".aac"):
                filetype = "audio"
            else:
                filetype = "file"
            # create download URL (served by download_file route)
            download_url = url_for("download_file", filename=filename)
        history_items.append({
            "id": _id,
            "platform": platform,
            "url": url,
            "filename": filename,
            "downloaded_at": downloaded_at,
            "filetype": filetype,
            "download_url": download_url
        })

    return render_template("history.html", history=history_items)

@app.route("/download/<path:filename>")
def download_file(filename):
    # Find the file inside DOWNLOAD_FOLDER and subfolders (prevents path traversal)
    target_basename = os.path.basename(filename)
    # Search persistent downloads first, then temporary zip area
    search_dirs = [str(DOWNLOAD_FOLDER), str(DOWNLOAD_BASE)]
    for base in search_dirs:
        for root, dirs, files in os.walk(base):
            if target_basename in files:
                full = os.path.join(root, target_basename)
                return send_file(full, as_attachment=True)
    abort(404)

# ================= PROFILE =================
@app.route("/profile", methods=["GET", "POST"], endpoint="profile")
def profile():
    if request.method == "POST":
        profile_user = request.form.get("profile_user", "").strip()
        max_posts = request.form.get("max_posts")
        max_posts = int(max_posts) if max_posts and max_posts.isdigit() else None

        if not profile_user:
            flash("Enter an Instagram username.", "error")
            return redirect(url_for("instagram"))

        L = Instaloader(save_metadata=False, download_comments=False, compress_json=False)
        logged_in = _try_load_instaloader_session(L)

        try:
            profile_obj = Profile.from_username(L.context, profile_user)
        except Exception as e:
            flash(f"Could not fetch profile: {e}", "error")
            return redirect(url_for("instagram"))

        if getattr(profile_obj, "is_private", False) and not logged_in:
            return redirect(
                url_for(
                    "insta_login",
                    next=f"profile:{profile_user}",
                    message="Profile is private; please login to continue.",
                    url=""
                )
            )

        tmpdir, media_files = download_profile_to_temp(
            L, profile_obj, max_posts=max_posts
        )

        if not media_files:
            shutil.rmtree(tmpdir, ignore_errors=True)
            flash("No downloadable media found for this profile.", "error")
            return redirect(url_for("instagram"))

        # ✅ YAHI SE tumhara missing part
        saved_files = []

        for p in media_files:
            dest = DOWNLOAD_FOLDER / p.name
            shutil.copy2(p, dest)
            saved_files.append(dest.name)
            add_history(
                "instagram_profile",
                f"https://instagram.com/{profile_user}/",
                p.name
            )

        _schedule_cleanup([tmpdir], delay=10)

        return render_template(
            "profile_view.html",
            profile_name=profile_user,
            is_private=profile_obj.is_private,
            files=saved_files
        )

    # GET request
    return redirect(url_for("instagram"))
@app.route("/user_photo/<filename>")
def user_photo(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ================= LOGOUT =================

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/logout_user")
def logout_user():
    session.clear()
    return redirect(url_for("index"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or not password or not confirm:
            return render_template("register.html", error="All fields are required.")
        if password != confirm:
            return render_template("register.html", error="Passwords do not match.")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username=?", (username,))
        if c.fetchone():
            conn.close()
            return render_template("register.html", error="Username already exists.")
        password_hash = generate_password_hash(password)
        created_at = datetime.utcnow().isoformat()
        c.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, created_at)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("login", message="Registration successful. Please log in."))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM users WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()
        if row and check_password_hash(row[1], password):
            session["user_id"] = row[0]
            session["username"] = username
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Invalid username or password.")
    message = request.args.get("message")
    return render_template("login.html", message=message)

@app.route("/profile_page", methods=["GET", "POST"])
def profile_page():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        # Handle photo upload or removal
        if "remove_photo" in request.form:
            c.execute("UPDATE users SET photo=NULL WHERE id=?", (user_id,))
            conn.commit()
        elif "photo" in request.files:
            file = request.files["photo"]
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = f"{session['username']}_profile.{ext}"
                filepath = UPLOAD_FOLDER / filename
                file.save(str(filepath))
                c.execute("UPDATE users SET photo=? WHERE id=?", (filename, user_id))
                conn.commit()
    c.execute("SELECT username, created_at, photo FROM users WHERE id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    username = row[0] if row else ""
    created_at = row[1] if row else ""
    photo = row[2] if row else None
    return render_template("profile_page.html", username=username, created_at=created_at, photo=photo)

# --- Instagram Login for User Session ---
@app.route("/ig_login", methods=["GET", "POST"])
def ig_login():
    if "user_id" not in session:
        return redirect(url_for("login"))
    message = None
    status = None
    if request.method == "POST":
        ig_username = request.form.get("ig_username", "").strip()
        ig_password = request.form.get("ig_password", "")
        if not ig_username or not ig_password:
            message = "Instagram username and password required."
            status = "fail"
        else:
            L = Instaloader(save_metadata=False, download_comments=False, compress_json=False)
            try:
                L.login(ig_username, ig_password)
                session_file = SESSION_DIR / f"{session['username']}_ig.session"
                L.save_session_to_file(filename=str(session_file))
                message = "Instagram login successful."
                status = "success"
            except instaloader.exceptions.BadCredentialsException:
                message = "Invalid Instagram username or password."
                status = "fail"
            except instaloader.exceptions.TwoFactorAuthRequiredException:
                message = "Two-factor authentication is enabled. This tool does not support 2FA logins."
                status = "fail"
            except instaloader.exceptions.ConnectionException as e:
                message = f"Connection error: {e}"
                status = "fail"
            except Exception as e:
                message = f"Instagram login failed: {e}"
                status = "fail"
    # Always provide a message for the template if status is fail
    if status == "fail" and not message:
        message = "Instagram login failed. Please try again."
    if status == "success" and not message:
        message = "Instagram login successful."
    return render_template("ig_login.html", message=message, status=status)

# --- Instagram Private Media Download ---
@app.route("/ig_download", methods=["GET", "POST"])
def ig_download():
    if "user_id" not in session:
        return redirect(url_for("login"))
    message = None
    files = []
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            message = "Please enter a valid Instagram post/reel/story URL or username."
        else:
            session_file = SESSION_DIR / f"{session['username']}_ig.session"
            if not session_file.exists():
                message = "You must log in to Instagram first."
            else:
                L = Instaloader(save_metadata=False, download_comments=False, compress_json=False)
                try:
                    L.load_session_from_file(None, filename=str(session_file))
                except Exception:
                    message = "Instagram session invalid or expired. Please log in again."
                    return render_template("ig_download.html", message=message)
                try:
                    # If URL is a profile, download latest posts
                    if re.match(r"^https?://(www\.)?instagram\.com/[^/]+/?$", url):
                        username = url.rstrip("/").split("/")[-1]
                        profile = Profile.from_username(L.context, username)
                        if profile.is_private and not profile.followed_by_viewer:
                            message = "You do not have access to this private profile."
                        else:
                            tmpdir, media_files = download_profile_to_temp(L, profile, max_posts=5)
                            files = [str(p) for p in media_files]
                            _schedule_cleanup([tmpdir], delay=10)
                    else:
                        # Assume post/reel/story URL
                        shortcode = extract_shortcode(url)
                        if not shortcode:
                            message = "Invalid Instagram URL."
                        else:
                            post = Post.from_shortcode(L.context, shortcode)
                            if post.owner_profile.is_private and not post.owner_profile.followed_by_viewer:
                                message = "You do not have access to this private post."
                            else:
                                tmpdir, media_files = download_post_to_temp(L, post)
                                files = [str(p) for p in media_files]
                                _schedule_cleanup([tmpdir], delay=10)
                except instaloader.exceptions.BadCredentialsException:
                    message = "Instagram login failed. Please log in again."
                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    message = "Access denied: You are not an approved follower of this private account."
                except instaloader.exceptions.ConnectionException as e:
                    message = f"Connection error: {e}"
                except instaloader.exceptions.QueryReturnedNotFoundException:
                    message = "Media not found or you do not have access."
                except instaloader.exceptions.RateLimitException:
                    message = "Instagram rate limit reached. Please try again later."
                except Exception as e:
                    message = f"Error: {e}"
    return render_template("ig_download.html", message=message, files=files)

# --- Instagram OAuth Login and Media Download ---
INSTAGRAM_CLIENT_ID = "YOUR_INSTAGRAM_APP_ID"  # Replace with your Instagram App ID
INSTAGRAM_CLIENT_SECRET = "YOUR_INSTAGRAM_APP_SECRET"  # Replace with your Instagram App Secret
INSTAGRAM_REDIRECT_URI = "http://localhost:5000/instagram_callback"  # Set this in your Instagram App settings

@app.route("/login_with_instagram")
def login_with_instagram():
    oauth_url = (
        "https://api.instagram.com/oauth/authorize"
        "?client_id={client_id}"
        "&redirect_uri={redirect_uri}"
        "&scope=user_profile,user_media"
        "&response_type=code"
    ).format(
        client_id=INSTAGRAM_CLIENT_ID,
        redirect_uri=INSTAGRAM_REDIRECT_URI
    )
    return redirect(oauth_url)

@app.route("/instagram_callback")
def instagram_callback():
    code = request.args.get("code")
    if not code:
        return "Authorization failed.", 400

    # Exchange code for access token
    token_url = "https://api.instagram.com/oauth/access_token"
    data = {
        "client_id": INSTAGRAM_CLIENT_ID,
        "client_secret": INSTAGRAM_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": INSTAGRAM_REDIRECT_URI,
        "code": code,
    }
    resp = requests.post(token_url, data=data)
    if resp.status_code != 200:
        return "Failed to get access token.", 400
    token_info = resp.json()
    access_token = token_info.get("access_token")
    user_id = token_info.get("user_id")
    if not access_token or not user_id:
        return "Invalid token response.", 400

    session["ig_access_token"] = access_token
    session["ig_user_id"] = user_id
    return redirect(url_for("instagram_media"))

@app.route("/instagram_media")
def instagram_media():
    access_token = session.get("ig_access_token")
    user_id = session.get("ig_user_id")
    if not access_token or not user_id:
        return redirect(url_for("login_with_instagram"))

    # Fetch user's media (private and public)
    media_url = (
        f"https://graph.instagram.com/me/media"
        f"?fields=id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
        f"&access_token={access_token}"
    )
    resp = requests.get(media_url)
    if resp.status_code != 200:
        return "Failed to fetch media.", 400
    media_data = resp.json().get("data", [])
    return render_template("instagram_media.html", media=media_data)

# === NEW LOGIN/REGISTER ROUTES ===
@app.route("/new_login", methods=["GET", "POST"])
def new_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM users WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()
        if row and check_password_hash(row[1], password):
            session["user_id"] = row[0]
            session["username"] = username
            return redirect(url_for("index"))
        else:
            return render_template("new_login.html", error="Invalid username or password.")
    message = request.args.get("message")
    return render_template("new_login.html", message=message)

@app.route("/new_register", methods=["GET", "POST"])
def new_register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or not password or not confirm:
            return render_template("new_register.html", error="All fields are required.")
        if password != confirm:
            return render_template("new_register.html", error="Passwords do not match.")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username=?", (username,))
        if c.fetchone():
            conn.close()
            return render_template("new_register.html", error="Username already exists.")
        password_hash = generate_password_hash(password)
        created_at = datetime.utcnow().isoformat()
        c.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, created_at)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("new_login", message="Registration successful. Please log in."))
    return render_template("new_register.html")

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
