import os
import json
import random
import base64
import io
import re
import threading
import urllib.request
from urllib.parse import urlparse, unquote
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_from_directory)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename
import phonenumbers
from config import Config
try:
    import cloudinary
    import cloudinary.uploader
except ImportError:  # package not installed -> local disk storage
    cloudinary = None
from sqlalchemy import or_
from models import (db, User, Connection, Message, Call, FanCard,
                    FanCardDesign, VoiceEffect, VideoLibrary, Notification,
                    LipSyncVideo, LipSyncSession)
from database import init_db
from auth import (current_user, login_required, admin_required,
                  public_figure_required, user_required)
from lip_sync import LipSyncProcessor
from lip_sync_video_processor import LipSyncVideoGenerator, SessionRecorder

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config.from_object(Config)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
# With Postgres under gevent, make the driver cooperative so database calls
# don't block websockets/calls for other users.
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
    try:
        from psycogreen.gevent import patch_psycopg
        patch_psycopg()
    except ImportError:
        pass
db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# Initialize lip sync components
try:
    lip_sync_processor = LipSyncProcessor()
    video_generator = LipSyncVideoGenerator(app.config["UPLOAD_FOLDER"])
except:
    # Fallback if lip sync dependencies not available
    lip_sync_processor = None
    video_generator = None

# Session recorders for lip sync sessions (session_id -> SessionRecorder)
lip_sync_sessions = {}


def allowed_file(filename, kinds=("image",)):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    if "image" in kinds and ext in app.config["ALLOWED_IMAGE_EXT"]:
        return True
    if "video" in kinds and ext in app.config["ALLOWED_VIDEO_EXT"]:
        return True
    if "audio" in kinds and ext in app.config.get("ALLOWED_AUDIO_EXT", ("mp3", "wav", "m4a", "aac")):
        return True
    return False


def generate_card_expiry_and_code():
    """Fan card expiry is always exactly 1 year from creation, and the
    special code is always a fixed 7-character random alphanumeric string.
    Users never choose either - both are system-generated."""
    import string
    from datetime import timedelta
    expiry = (datetime.utcnow().replace(microsecond=0) + timedelta(days=365)).strftime("%Y-%m-%d")
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=7))
    return expiry, code


# ---------------- Media storage (Cloudinary, with local-disk fallback) ----------------
def _setup_cloudinary():
    if cloudinary is None:
        return False
    name = key = secret = None
    url = app.config.get("CLOUDINARY_URL")
    if url:
        p = urlparse(url)
        if p.scheme == "cloudinary":
            name = p.netloc.rsplit("@", 1)[-1]
            key, secret = p.username, p.password
    name = name or app.config.get("CLOUDINARY_CLOUD_NAME")
    key = key or app.config.get("CLOUDINARY_API_KEY")
    secret = secret or app.config.get("CLOUDINARY_API_SECRET")
    if not (name and key and secret):
        return False
    cloudinary.config(cloud_name=name, api_key=key, api_secret=secret, secure=True)
    return True


USE_CLOUDINARY = _setup_cloudinary()
print("[BOOT] Media storage:", "Cloudinary" if USE_CLOUDINARY else "local disk (static/uploads)")


def save_local(file, kinds=("image",)):
    """Save an uploaded file to the local uploads folder; returns its /static/uploads URL."""
    if not file or file.filename == "" or not allowed_file(file.filename, kinds):
        return ""
    fname = "%d_%s" % (int(datetime.utcnow().timestamp()), secure_filename(file.filename))
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
    return "/static/uploads/" + fname


def save_upload(file, kinds=("image",)):
    """Store an uploaded file and return its public URL ("" if rejected or failed)."""
    if not USE_CLOUDINARY:
        return save_local(file, kinds)
    if not file or file.filename == "" or not allowed_file(file.filename, kinds):
        return ""
    try:
        file.stream.seek(0)
        res = cloudinary.uploader.upload(
            file.stream, resource_type="auto",
            folder=app.config["CLOUDINARY_FOLDER"], overwrite=False)
        return res.get("secure_url", "")
    except Exception as e:
        print("[STORAGE] Cloudinary upload failed:", e)
        return ""


def publish_local_file(path):
    """Publish a file generated on the server (fan card PNG, lip-sync video).
    With Cloudinary the local copy is removed after upload. Returns the public URL or ""."""
    if not os.path.exists(path):
        return ""
    if not USE_CLOUDINARY:
        return "/static/uploads/" + os.path.basename(path)
    try:
        res = cloudinary.uploader.upload(
            path, resource_type="auto",
            folder=app.config["CLOUDINARY_FOLDER"], overwrite=False)
        os.remove(path)
        return res.get("secure_url", "")
    except Exception as e:
        print("[STORAGE] Cloudinary upload failed:", e)
        return ""


def open_media(ref):
    """Open a stored media reference (Cloudinary URL or local /static/uploads path)
    for reading. Returns a path/file-like object, or None if unavailable."""
    if not ref:
        return None
    try:
        if ref.startswith(("http://", "https://")):
            host = urlparse(ref).hostname or ""
            if not host.endswith("cloudinary.com"):
                return None
            with urllib.request.urlopen(ref, timeout=15) as r:
                return io.BytesIO(r.read())
        path = ref.lstrip("/")
        return path if os.path.exists(path) else None
    except Exception as e:
        print("[STORAGE] Could not read media:", e)
        return None


_CLD_URL_RE = re.compile(r"^https?://res\.cloudinary\.com/[^/]+/(image|video|raw)/upload/(?:v\d+/)?(.+)$")


def delete_media(ref):
    """Best-effort removal of a stored file (Cloudinary asset or local upload)."""
    if not ref:
        return
    try:
        m = _CLD_URL_RE.match(ref)
        if m:
            if not USE_CLOUDINARY:
                return
            rtype, public_id = m.group(1), unquote(m.group(2))
            last = public_id.rsplit("/", 1)[-1]
            if rtype != "raw" and "." in last:
                public_id = public_id.rsplit(".", 1)[0]
            cloudinary.uploader.destroy(public_id, resource_type=rtype, invalidate=True)
        elif ref.startswith("/static/uploads/"):
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(ref)))
    except Exception as e:
        print("[STORAGE] Could not delete media:", e)


def generate_fan_card_png(design, card):
    """Composite the design's background with the user's submitted values
    at the field positions saved by the admin editor. Returns the saved
    file's public URL, or "" if generation isn't possible (no background
    or no layout saved yet)."""
    if not design.preview or not design.design_data:
        return ""
    try:
        fields = json.loads(design.design_data)
    except Exception:
        return ""
    if not fields:
        return ""

    bg_src = open_media(design.preview)
    if not bg_src:
        return ""

    try:
        base_img = Image.open(bg_src).convert("RGBA")
        W, H = base_img.size
        draw = ImageDraw.Draw(base_img)

        def font_for(size):
            try:
                return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
            except Exception:
                return ImageFont.load_default()

        text_values = {
            "name": card.name or "",
            "expiry_date": card.expiry_date or "",
            "special_code": card.special_code or "",
        }
        for key, val in text_values.items():
            f = fields.get(key)
            if not f or not f.get("enabled") or not val:
                continue
            x = f["x"] / 100 * W
            y = f["y"] / 100 * H
            w = f["w"] / 100 * W
            font = font_for(max(8, int(f.get("fontPct", 5) / 100 * H)))
            color = f.get("color", "#ffffff")
            align = f.get("align", "left")
            bbox = draw.textbbox((0, 0), val, font=font)
            text_w = bbox[2] - bbox[0]
            draw_x = x
            if align == "center":
                draw_x = x + (w - text_w) / 2
            elif align == "right":
                draw_x = x + w - text_w
            draw.text((draw_x, y), val, font=font, fill=color)

        photo_field = fields.get("photo")
        if photo_field and photo_field.get("enabled") and card.photo:
            photo_src = open_media(card.photo)
            if photo_src:
                px = int(photo_field["x"] / 100 * W)
                py = int(photo_field["y"] / 100 * H)
                pw = int(photo_field["w"] / 100 * W)
                ph = int(photo_field["h"] / 100 * H)
                user_photo = Image.open(photo_src).convert("RGBA")
                user_photo = user_photo.resize((max(1, pw), max(1, ph)))
                base_img.paste(user_photo, (px, py), user_photo)

        out_name = "%d_fancard_%d.png" % (int(datetime.utcnow().timestamp()), card.id)
        out_path = os.path.join(app.config["UPLOAD_FOLDER"], out_name)
        base_img.convert("RGB").save(out_path, "PNG")
        return publish_local_file(out_path)
    except Exception as e:
        print("[FAN CARD] PNG generation failed:", e)
        return ""


def humanize_count(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            v = n / div
            s = f"{v:.1f}".rstrip("0").rstrip(".")
            return f"{s}{unit}"
    return str(n)


def get_country_from_phone(phone_number):
    """Extract country code and name from phone number"""
    try:
        parsed = phonenumbers.parse(phone_number, None)
        country_code = phonenumbers.region_code_for_number(parsed)
        country_name = phonenumbers.country_names_for_number(parsed, "en") or ""
        return country_code, country_name
    except:
        return "", ""


def format_phone_number(phone_number):
    """Format phone number with + and - symbols"""
    try:
        parsed = phonenumbers.parse(phone_number, None)
        formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        return formatted
    except:
        return phone_number


def normalize_phone_for_verification(phone_number):
    """Remove all symbols from phone for verification - works with or without +, -, spaces"""
    import re
    # Remove all non-digit characters except +
    normalized = re.sub(r'[^\d+]', '', phone_number)
    # Remove + if present (we'll just use digits)
    normalized = normalized.replace('+', '')
    return normalized


def get_device_info():
    """Extract device info from user agent"""
    user_agent = request.headers.get('User-Agent', '')
    device_info = {
        'user_agent': user_agent,
        'ip': request.remote_addr,
    }
    return json.dumps(device_info)


def generate_verification_code():
    """Generate 6-digit verification code"""
    return str(random.randint(100000, 999999))


def _ice_servers():
    """WebRTC ICE servers. Defaults to Google STUN; set ICE_SERVERS_JSON to add a TURN
    relay (needed for many mobile networks), e.g.
    [{"urls":"stun:stun.l.google.com:19302"},
     {"urls":"turn:HOST:3478","username":"USER","credential":"PASS"}]"""
    default = [{"urls": "stun:stun.l.google.com:19302"}]
    raw = app.config.get("ICE_SERVERS_JSON") or ""
    if not raw:
        return default
    try:
        servers = json.loads(raw)
        return servers if isinstance(servers, list) and servers else default
    except ValueError:
        print("[CALLS] ICE_SERVERS_JSON is not valid JSON; using default STUN")
        return default


@app.context_processor
def inject_globals():
    return {"current_user": current_user(), "now": datetime.utcnow(), "ice_servers": _ice_servers()}


app.jinja_env.filters["humanize"] = humanize_count
app.jinja_env.filters["from_json"] = lambda s: __import__("json").loads(s) if s else {}


# ---------------- PWA ----------------
@app.route("/manifest.webmanifest", endpoint="pwa.manifest")
def pwa_manifest():
    resp = send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/sw.js", endpoint="pwa.sw")
def pwa_sw():
    # Served from the site root so the worker can control every page.
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/offline", endpoint="pwa.offline")
def pwa_offline():
    # Public and user-independent: the service worker caches this page.
    return render_template("offline.html")


@app.route("/ping", endpoint="pwa.ping")
def pwa_ping():
    resp = app.response_class(status=204)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- Root ----------------
@app.route("/", endpoint="home_router")
def home_router():
    u = current_user()
    if not u:
        return redirect(url_for("auth.login"))
    if u.is_admin:
        return redirect(url_for("admin.dashboard"))
    if u.is_public_figure:
        return redirect(url_for("public.dashboard"))
    return redirect(url_for("user.home"))


# ---------------- Auth ----------------
@app.route("/login", methods=["GET", "POST"], endpoint="auth.login")
def login():
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        pw = request.form.get("password", "")
        u = User.query.filter((User.email == ident) | (User.phone == ident)).first()
        if u and u.check_password(pw) and u.status == "active" and u.role != "admin":
            session["user_id"] = u.id
            if u.is_public_figure:
                return redirect(url_for("public.dashboard"))
            return redirect(url_for("user.home"))
        flash("Invalid credentials.", "error")
    return render_template("auth/login.html")


@app.route("/register", methods=["GET", "POST"], endpoint="auth.register")
def register():
    if request.method == "POST":
        fn = request.form.get("full_name", "").strip()
        em = request.form.get("email", "").strip().lower()
        ph = request.form.get("phone", "").strip()
        pw = request.form.get("password", "")
        cp = request.form.get("confirm_password", "")
        if not all([fn, em, ph, pw]):
            flash("All fields required.", "error")
        elif pw != cp:
            flash("Passwords do not match.", "error")
        elif User.query.filter_by(email=em).first():
            flash("Email already registered.", "error")
        else:
            # Format phone number with international format
            formatted_phone = format_phone_number(ph)
            
            # Check if phone already exists (by normalized form)
            normalized_input = normalize_phone_for_verification(ph)
            existing_users = User.query.all()
            for existing_u in existing_users:
                if normalize_phone_for_verification(existing_u.phone) == normalized_input:
                    flash("Phone already registered.", "error")
                    return render_template("auth/register.html")
            
            country_code, country_name = get_country_from_phone(ph)
            verification_code = generate_verification_code()
            u = User(full_name=fn, email=em, phone=formatted_phone, role="user", status="active",
                    country_code=country_code, country_name=country_name,
                    device_info=get_device_info(), ip_address=request.remote_addr,
                    phone_verification_code=verification_code)
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            session["user_id"] = u.id
            session["phone_verify_required"] = True
            session["verification_code"] = verification_code
            # Return verification code to show in popup (not sent anywhere)
            return redirect(url_for("auth.verify_phone"))
    return render_template("auth/register.html")


@app.route("/logout", endpoint="auth.logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@app.route("/verify-phone", methods=["GET", "POST"], endpoint="auth.verify_phone")
@login_required
def verify_phone():
    u = current_user()
    if u.phone_verified:
        return redirect(url_for("user.home"))
    
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        # Remove all non-digit characters from entered code
        code_normalized = ''.join(c for c in code if c.isdigit())
        
        if code_normalized == u.phone_verification_code:
            u.phone_verified = True
            u.last_login = datetime.utcnow()
            db.session.commit()
            flash(f"Phone verified! Country: {u.country_name}", "success")
            return redirect(url_for("user.home"))
        else:
            flash("Invalid verification code.", "error")
    
    # Format phone for display with international format
    formatted_phone = format_phone_number(u.phone)
    
    return render_template("auth/verify_phone.html", 
                         phone=formatted_phone, country=u.country_name)


@app.route("/api/verification-code", methods=["GET"])
def get_verification_code():
    """API endpoint to get verification code - displays in popup modal"""
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    if u.phone_verified:
        return jsonify({"error": "Already verified"}), 400
    
    return jsonify({
        "code": u.phone_verification_code,
        "phone": format_phone_number(u.phone),
        "country": u.country_name
    })


@app.route("/api/verify-code", methods=["POST"])
def verify_code_api():
    """API endpoint to verify the code entered in popup"""
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    if u.phone_verified:
        return jsonify({"error": "Already verified"}), 400
    
    code = request.json.get("code", "").strip()
    code_normalized = ''.join(c for c in code if c.isdigit())
    
    if code_normalized == u.phone_verification_code:
        u.phone_verified = True
        u.last_login = datetime.utcnow()
        db.session.commit()
        return jsonify({
            "success": True,
            "message": f"Phone verified! Country: {u.country_name}"
        })
    else:
        return jsonify({
            "success": False,
            "message": "Invalid verification code"
        }), 400


@app.route("/admin", methods=["GET", "POST"], endpoint="admin.index")
def admin_index():
    u = current_user()
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        pw = request.form.get("password", "")
        user = User.query.filter_by(email=ident).first()
        if user and user.is_admin and user.check_password(pw):
            session["user_id"] = user.id
            return redirect(url_for("admin.dashboard"))
        flash("Invalid admin credentials.", "error")
        return render_template("admin/login.html")
    if not u:
        return render_template("admin/login.html")
    if u.is_admin:
        return redirect(url_for("admin.dashboard"))
    session.clear()
    flash("Admin access required.", "error")
    return render_template("admin/login.html")


@app.route("/admin/login", methods=["GET", "POST"], endpoint="admin.login")
def admin_login():
    return admin_index()


# ---------------- User ----------------
@app.route("/user/home", endpoint="user.home")
@user_required
def user_home():
    u = current_user()
    pf = User.query.get(u.assigned_public_figure_id) if u.assigned_public_figure_id else None
    notifs = Notification.query.filter_by(user_id=u.id).order_by(Notification.created_at.desc()).limit(10).all()
    return render_template("user/home.html", user=u, public_figure=pf, notifications=notifs)


@app.route("/user/connect", methods=["GET", "POST"], endpoint="user.connect")
@user_required
def user_connect():
    if request.method == "POST":
        # The person types the full international number (country code included);
        # compare digits only so spaces, dashes and a leading + don't matter.
        wanted = normalize_phone_for_verification(request.form.get("phone", ""))
        pf = None
        if wanted:
            for cand in User.query.filter_by(role="public_figure", status="active").all():
                if normalize_phone_for_verification(cand.phone or "") == wanted:
                    pf = cand
                    break
        if not pf:
            return jsonify({"ok": False, "error": "No active public figure found."})
        return jsonify({"ok": True, "public_figure": {
            "id": pf.id, "name": pf.full_name,
            "photo": pf.profile_photo or "", "verified": pf.verified}})
    return render_template("user/connect.html", user=current_user())


@app.route("/user/connect/confirm", methods=["POST"], endpoint="user.connect_confirm")
@user_required
def user_connect_confirm():
    u = current_user()
    data = request.get_json(silent=True) or {}
    pf_id = data.get("public_figure_id") or request.form.get("public_figure_id")
    pf = User.query.get(int(pf_id)) if pf_id and str(pf_id).isdigit() else None
    if not pf or pf.role != "public_figure" or pf.status != "active":
        return jsonify({"ok": False, "error": "Invalid public figure."})
    Connection.query.filter_by(user_id=u.id, active=True).update({"active": False})
    db.session.add(Connection(user_id=u.id, public_figure_id=pf.id,
                              assigned_by=pf.id, active=True))
    u.assigned_public_figure_id = pf.id
    db.session.add(Notification(user_id=u.id, title="Connected",
                                message="You are now connected to %s." % pf.full_name))
    db.session.commit()
    return jsonify({"ok": True, "redirect": url_for("user.chat_with_pf")})


@app.route("/user/chat", endpoint="user.chat_with_pf")
@user_required
def user_chat_with_pf():
    u = current_user()
    if not u.assigned_public_figure_id:
        return redirect(url_for("user.home"))
    pf = User.query.get(u.assigned_public_figure_id)
    if not pf:
        flash("Your connected public figure is no longer available.", "error")
        return redirect(url_for("user.home"))
    msgs = Message.query.filter(
        ((Message.sender_id == u.id) & (Message.receiver_id == pf.id)) |
        ((Message.sender_id == pf.id) & (Message.receiver_id == u.id))
    ).order_by(Message.created_at.asc()).limit(500).all()
    Message.query.filter_by(sender_id=pf.id, receiver_id=u.id, read_status=False).update({"read_status": True})
    db.session.commit()
    return render_template("user/chat.html", user=u, peer=pf, messages=msgs,
                           peer_online=is_user_online(pf.id))


@app.route("/user/calls", endpoint="user.calls")
@user_required
def user_calls():
    u = current_user()
    pf = User.query.get(u.assigned_public_figure_id) if u.assigned_public_figure_id else None
    calls = Call.query.filter((Call.caller_id == u.id) | (Call.receiver_id == u.id))\
                      .order_by(Call.created_at.desc()).limit(50).all()
    return render_template("user/calls.html", user=u, public_figure=pf, calls=calls)


@app.route("/user/profile", methods=["GET", "POST"], endpoint="user.profile")
@user_required
def user_profile():
    u = current_user()
    if request.method == "POST":
        u.full_name = request.form.get("full_name", u.full_name).strip()
        u.bio = request.form.get("bio", u.bio)
        photo = request.files.get("photo")
        if photo and photo.filename:
            url = save_upload(photo)
            if url:
                u.profile_photo = url
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("user.profile"))
    pf = User.query.get(u.assigned_public_figure_id) if u.assigned_public_figure_id else None
    cards = FanCard.query.filter_by(user_id=u.id).order_by(FanCard.created_at.desc()).all()
    return render_template("user/profile.html", user=u, public_figure=pf, cards=cards)


@app.route("/user/fan-card", methods=["GET", "POST"], endpoint="user.fan_card")
@user_required
def user_fan_card():
    u = current_user()
    if not u.assigned_public_figure_id:
        return redirect(url_for("user.home"))
    pf = User.query.get(u.assigned_public_figure_id)
    if not pf:
        return redirect(url_for("user.home"))
    if request.method == "POST":
        design_id = request.form.get("design_id", "")
        card_name = request.form.get("name", "").strip()
        design = None
        if design_id.isdigit():
            design = FanCardDesign.query.get(int(design_id))
        if not design or not design.active or design.assigned_public_figure_id != pf.id:
            flash("Please choose a valid design.", "error")
        else:
            photo_url = ""
            photo_file = request.files.get("photo")
            if photo_file and photo_file.filename:
                photo_url = save_upload(photo_file) or ""
            expiry, code = generate_card_expiry_and_code()
            card = FanCard(user_id=u.id, public_figure_id=pf.id, design_id=design.id,
                            name=card_name, photo=photo_url,
                            expiry_date=expiry, special_code=code)
            db.session.add(card)
            db.session.commit()
            png_url = generate_fan_card_png(design, card)
            if png_url:
                card.generated_card = png_url
                db.session.commit()
            flash("Fan card created! Awaiting approval.", "info")
            return redirect(url_for("user.profile"))
    designs = FanCardDesign.query.filter_by(assigned_public_figure_id=u.assigned_public_figure_id, active=True).all()
    return render_template("user/fan_card.html", user=u, public_figure=pf, designs=designs)


@app.route("/user/profile/<int:pf_id>", endpoint="user.view_pf")
@user_required
def user_view_pf(pf_id):
    pf = User.query.get_or_404(pf_id)
    if pf.role != "public_figure" or pf.status != "active":
        return redirect(url_for("user.home"))
    return render_template("user/public_figure_profile.html", pf=pf)


# ---------------- Public Figure ----------------
@app.route("/public/dashboard", endpoint="public.dashboard")
@public_figure_required
def public_dashboard():
    pf = current_user()
    followers_count = pf.followers_count or 0
    likes_count = pf.likes_count or 0
    fans_count = Connection.query.filter_by(public_figure_id=pf.id, active=True).count()
    chats_count = Message.query.filter((Message.sender_id == pf.id) | (Message.receiver_id == pf.id)).count()
    calls_count = Call.query.filter((Call.caller_id == pf.id) | (Call.receiver_id == pf.id)).count()
    
    fan_rows = []
    connections = Connection.query.filter_by(public_figure_id=pf.id, active=True).all()
    for conn in connections:
        user = User.query.get(conn.user_id)
        if user:
            last_msg = Message.query.filter(
                ((Message.sender_id == pf.id) & (Message.receiver_id == user.id)) |
                ((Message.sender_id == user.id) & (Message.receiver_id == pf.id))
            ).order_by(Message.created_at.desc()).first()
            unread = Message.query.filter_by(sender_id=user.id, receiver_id=pf.id, read_status=False).count()
            fan_rows.append(type('obj', (object,), {'user': user, 'last': last_msg, 'unread': unread})())
    
    return render_template("public/dashboard.html", public_figure=pf, pf=pf, 
                         followers_count=followers_count, likes_count=likes_count,
                         fans_count=fans_count, chats_count=chats_count, calls_count=calls_count,
                         fan_rows=fan_rows)


@app.route("/public/calls", endpoint="public.calls")
@public_figure_required
def public_calls():
    pf = current_user()
    calls = Call.query.filter((Call.caller_id == pf.id) | (Call.receiver_id == pf.id))\
                       .order_by(Call.created_at.desc()).limit(50).all()
    peer_ids = {c.caller_id if c.caller_id != pf.id else c.receiver_id for c in calls}
    peers = {u.id: u for u in User.query.filter(User.id.in_(peer_ids)).all()} if peer_ids else {}
    return render_template("public/calls.html", pf=pf, calls=calls, peers=peers)


@app.route("/public/chat", endpoint="public.chat")
@public_figure_required
def public_chat():
    pf = current_user()
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return redirect(url_for("public.dashboard"))

    peer = User.query.get(user_id)
    conn = Connection.query.filter_by(public_figure_id=pf.id, user_id=user_id, active=True).first() if peer else None
    if not peer or not conn:
        flash("That follower is not connected to you.", "error")
        return redirect(url_for("public.dashboard"))

    msgs = Message.query.filter(
        ((Message.sender_id == pf.id) & (Message.receiver_id == peer.id)) |
        ((Message.sender_id == peer.id) & (Message.receiver_id == pf.id))
    ).order_by(Message.created_at.asc()).limit(500).all()
    Message.query.filter_by(sender_id=peer.id, receiver_id=pf.id, read_status=False).update({"read_status": True})
    db.session.commit()
    return render_template("public/chat.html", public_figure=pf, pf=pf, peer=peer, messages=msgs)


@app.route("/public/profile", methods=["GET", "POST"], endpoint="public.profile")
@public_figure_required
def public_profile():
    pf = current_user()
    if request.method == "POST":
        pf.full_name = request.form.get("full_name", pf.full_name).strip()
        pf.bio = request.form.get("bio", pf.bio)
        photo = request.files.get("photo")
        if photo and photo.filename:
            url = save_upload(photo)
            if url:
                pf.profile_photo = url
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("public.profile"))
    return render_template("public/profile.html", pf=pf)



# In-memory store for face-analysis background job status
# { user_id: { 'status': 'running'|'done'|'error', 'summary': str,
#              'mouth_x': float|None, 'mouth_y': float|None } }
_face_analysis_jobs = {}


def _run_face_analysis(user_id, local_path):
    """
    Background thread: run full 3-D face/mouth analysis on the uploaded video,
    then persist the detected mouth centre back to the database.
    """
    _face_analysis_jobs[user_id] = {'status': 'running', 'summary': '', 'mouth_x': None, 'mouth_y': None}
    try:
        if lip_sync_processor is None:
            _face_analysis_jobs[user_id] = {
                'status': 'error',
                'summary': 'Lip-sync processor not available',
                'mouth_x': None, 'mouth_y': None,
            }
            return

        result = lip_sync_processor.analyze_video_face_map(local_path, sample_rate=5)

        mx, my = (None, None)
        if result.get('face_found') and result.get('best_mouth_center'):
            mx, my = result['best_mouth_center']

        with app.app_context():
            from models import User as _User
            pf = _User.query.get(user_id)
            if pf:
                if mx is not None:
                    pf.mouth_x = float(mx)
                    pf.mouth_y = float(my)
                db.session.commit()

        _face_analysis_jobs[user_id] = {
            'status': 'done',
            'summary': result.get('summary', ''),
            'mouth_x': float(mx) if mx is not None else None,
            'mouth_y': float(my) if my is not None else None,
        }
    except Exception as exc:
        _face_analysis_jobs[user_id] = {
            'status': 'error',
            'summary': str(exc),
            'mouth_x': None, 'mouth_y': None,
        }


@app.route("/public/call-video", methods=["GET", "POST"], endpoint="public.call_video")
@public_figure_required
def public_call_video():
    pf = current_user()
    if request.method == "POST":
        video = request.files.get("video")
        if video and video.filename:
            if not allowed_file(video.filename, kinds=("video",)):
                return jsonify({"ok": False, "error": "Invalid video file. Use mp4, webm, or mov."}), 400
            url = save_upload(video, kinds=("video",))
            if not url:
                return jsonify({"ok": False, "error": "Failed to save video."}), 400
            pf.call_video_url = url
            # Clear any old mouth mapping so the new analysis takes over
            pf.mouth_x = None
            pf.mouth_y = None
            db.session.commit()

            # ── Kick off background 3-D face analysis ─────────────────────
            # Resolve the local disk path to the video so OpenCV can read it.
            # save_upload() returns a URL (Cloudinary) or a local /static/uploads path.
            local_path = None
            parsed = urlparse(url)
            if parsed.scheme in ('', 'file') or not parsed.netloc:
                # local path
                local_path = os.path.join(app.root_path, url.lstrip('/'))
            elif 'cloudinary.com' in (parsed.netloc or ''):
                # For Cloudinary we download to a temp file for analysis
                try:
                    import tempfile
                    ext = os.path.splitext(parsed.path)[-1] or '.mp4'
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                    urllib.request.urlretrieve(url, tmp.name)
                    local_path = tmp.name
                except Exception:
                    local_path = None

            if local_path and os.path.exists(local_path):
                t = threading.Thread(
                    target=_run_face_analysis,
                    args=(pf.id, local_path),
                    daemon=True,
                )
                t.start()
                auto_detect = True
            else:
                auto_detect = False

            return jsonify({"ok": True, "video_url": url, "auto_detect": auto_detect})
        return jsonify({"ok": False, "error": "No video provided."}), 400
    return render_template("public/call_video.html", pf=pf)


@app.route("/public/call-video/face-status", methods=["GET"], endpoint="public.call_video_face_status")
@public_figure_required
def public_call_video_face_status():
    """Poll endpoint — front-end checks this after upload to track auto face-detection progress."""
    pf = current_user()
    job = _face_analysis_jobs.get(pf.id)
    if job is None:
        # No job running; return current saved state
        return jsonify({
            "status": "idle",
            "mouth_x": pf.mouth_x,
            "mouth_y": pf.mouth_y,
        })
    return jsonify({
        "status": job["status"],
        "summary": job.get("summary", ""),
        "mouth_x": job.get("mouth_x"),
        "mouth_y": job.get("mouth_y"),
    })


@app.route("/public/call-video/remove", methods=["POST"], endpoint="public.call_video_remove")
@public_figure_required
def public_call_video_remove():
    pf = current_user()
    pf.call_video_url = ""
    pf.mouth_x = None
    pf.mouth_y = None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/public/call-video/mouth", methods=["POST"], endpoint="public.call_video_mouth")
@public_figure_required
def public_call_video_mouth():
    pf = current_user()
    data = request.get_json(silent=True) or {}
    mx = data.get("mouth_x")
    my = data.get("mouth_y")
    if mx is None or my is None:
        return jsonify({"ok": False, "error": "Missing coordinates"}), 400
    try:
        pf.mouth_x = float(mx)
        pf.mouth_y = float(my)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- Admin ----------------
@app.route("/admin/dashboard", endpoint="admin.dashboard")
@admin_required
def admin_dashboard():
    total_users = User.query.filter_by(role="user").count()
    total_pfs = User.query.filter_by(role="public_figure").count()
    total_msgs = Message.query.count()
    total_calls = Call.query.count()
    connected = User.query.filter(User.role == "user", User.assigned_public_figure_id.isnot(None)).count()
    active_chats = db.session.query(Message.sender_id, Message.receiver_id).distinct().count()
    fan_cards = FanCard.query.count()
    pending_cards = FanCard.query.filter_by(status="pending").count()
    return render_template("admin/dashboard.html",
                          total_users=total_users, total_pfs=total_pfs,
                          total_msgs=total_msgs, total_calls=total_calls,
                          public_figures=total_pfs, connected=connected,
                          active_chats=active_chats, active_calls=0,
                          fan_cards=fan_cards, pending_cards=pending_cards)


@app.route("/admin/users", endpoint="admin.users_list")
@admin_required
def admin_users_list():
    users = User.query.filter_by(role="user").order_by(User.id.desc()).all()
    return render_template("admin/users.html", users=users)


@app.route("/admin/public-figures", endpoint="admin.public_figures_list")
@admin_required
def admin_public_figures_list():
    pfs = User.query.filter_by(role="public_figure").order_by(User.id.desc()).all()
    return render_template("admin/public_figures.html", pfs=pfs)


@app.route("/admin/public-figures/create", methods=["POST"], endpoint="admin.create_pf")
@admin_required
def admin_create_pf():
    fn = request.form.get("full_name", "").strip()
    em = request.form.get("email", "").strip().lower()
    ph = request.form.get("phone", "").strip()
    pw = request.form.get("password", "")
    if not all([fn, em, ph, pw]):
        flash("All fields required.", "error")
    elif User.query.filter_by(email=em).first():
        flash("Email already registered.", "error")
    elif User.query.filter_by(phone=ph).first():
        flash("Phone already registered.", "error")
    else:
        pf = User(full_name=fn, email=em, phone=ph, role="public_figure", status="active")
        pf.set_password(pw)
        photo = request.files.get("photo")
        if photo and photo.filename:
            url = save_upload(photo)
            if url:
                pf.profile_photo = url
        db.session.add(pf)
        db.session.commit()
        flash("Public figure created.", "success")
    return redirect(url_for("admin.public_figures_list"))


@app.route("/admin/users/<int:user_id>/upgrade", methods=["POST"], endpoint="admin.upgrade_user")
@admin_required
def admin_upgrade_user(user_id):
    u = User.query.get_or_404(user_id)
    u.role = "public_figure"
    db.session.commit()
    flash("User upgraded to public figure.", "success")
    return redirect(url_for("admin.users_list"))


@app.route("/admin/users/<int:user_id>/downgrade", methods=["POST"], endpoint="admin.downgrade_pf")
@admin_required
def admin_downgrade_pf(user_id):
    pf = User.query.get_or_404(user_id)
    pf.role = "user"
    db.session.commit()
    flash("Public figure downgraded to user.", "success")
    return redirect(request.referrer or url_for("admin.users_list"))


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"], endpoint="admin.toggle_user")
@admin_required
def admin_toggle_user(uid):
    u = User.query.get_or_404(uid)
    u.status = "suspended" if u.status == "active" else "active"
    db.session.commit()
    return jsonify({"ok": True, "status": u.status})


@app.route("/admin/users/<int:uid>/delete", methods=["POST"], endpoint="admin.delete_user")
@admin_required
def admin_delete_user(uid):
    """Permanently delete a user (or public figure) and everything tied to them."""
    me = current_user()
    u = User.query.get_or_404(uid)
    back = request.referrer or url_for("admin.users_list")

    if u.id == me.id or u.role == "admin":
        flash("Admin accounts can't be deleted.", "error")
        return redirect(back)

    name = u.full_name
    # Files to remove from storage once the DB rows are gone
    media_to_delete = [u.profile_photo, u.call_video_url]
    media_to_delete += [m.media_url for m in Message.query.filter(
        or_(Message.sender_id == uid, Message.receiver_id == uid)).all()]
    try:
        # Lip-sync data: sessions by this user, plus this account's videos and their sessions
        video_ids = [v.id for v in LipSyncVideo.query.filter_by(public_figure_id=uid).all()]
        LipSyncSession.query.filter_by(user_id=uid).delete(synchronize_session=False)
        if video_ids:
            LipSyncSession.query.filter(LipSyncSession.video_id.in_(video_ids)).delete(synchronize_session=False)
        LipSyncVideo.query.filter_by(public_figure_id=uid).delete(synchronize_session=False)

        # Rows that belong to this user
        Message.query.filter(or_(Message.sender_id == uid, Message.receiver_id == uid)).delete(synchronize_session=False)
        Call.query.filter(or_(Call.caller_id == uid, Call.receiver_id == uid)).delete(synchronize_session=False)
        FanCard.query.filter(or_(FanCard.user_id == uid, FanCard.public_figure_id == uid)).delete(synchronize_session=False)
        Connection.query.filter(or_(Connection.user_id == uid, Connection.public_figure_id == uid)).delete(synchronize_session=False)
        Notification.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # Nullable references from other rows: detach instead of deleting
        Connection.query.filter_by(assigned_by=uid).update({"assigned_by": None}, synchronize_session=False)
        User.query.filter_by(assigned_public_figure_id=uid).update({"assigned_public_figure_id": None}, synchronize_session=False)
        FanCardDesign.query.filter_by(assigned_public_figure_id=uid).update({"assigned_public_figure_id": None}, synchronize_session=False)

        db.session.delete(u)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("[ADMIN] delete_user failed:", e)
        flash("Could not delete user. Nothing was changed.", "error")
        return redirect(back)

    # Best-effort cleanup of the user's files (Cloudinary assets or local uploads)
    for ref in media_to_delete:
        delete_media(ref)

    flash("%s was deleted." % name, "success")
    return redirect(back)


@app.route("/admin/public-figures/<int:pf_id>/toggle-verified", methods=["POST"], endpoint="admin.toggle_verified")
@admin_required
def admin_toggle_verified(pf_id):
    pf = User.query.get_or_404(pf_id)
    if pf.role != "public_figure":
        return jsonify({"ok": False, "error": "Not a public figure."}), 400
    pf.verified = not pf.verified
    db.session.commit()
    return jsonify({"ok": True, "verified": pf.verified})


@app.route("/admin/assign", endpoint="admin.assign_page")
@admin_required
def admin_assign_page():
    pfs = User.query.filter_by(role="public_figure", status="active").all()
    users = User.query.filter_by(role="user", status="active").all()
    return render_template("admin/assign.html", pfs=pfs, users=users)


@app.route("/admin/assign-user", methods=["POST"], endpoint="admin.assign_user")
@admin_required
def admin_assign_user():
    u_id = request.form.get("user_id", "")
    pf_id = request.form.get("pf_id", "")
    u = User.query.get(int(u_id)) if u_id.isdigit() else None
    pf = User.query.get(int(pf_id)) if pf_id.isdigit() else None
    if u and pf and u.role == "user" and pf.role == "public_figure":
        Connection.query.filter_by(user_id=u.id, active=True).update({"active": False})
        db.session.add(Connection(user_id=u.id, public_figure_id=pf.id,
                                  assigned_by=current_user().id, active=True))
        u.assigned_public_figure_id = pf.id
        db.session.commit()
        flash("User assigned.", "success")
    else:
        flash("Invalid user or public figure selected.", "error")
    return redirect(url_for("admin.assign_page"))


@app.route("/admin/unassign-user/<int:uid>", methods=["POST"], endpoint="admin.unassign_user")
@admin_required
def admin_unassign_user(uid):
    u = User.query.get_or_404(uid)
    u.assigned_public_figure_id = None
    Connection.query.filter_by(user_id=u.id, active=True).update({"active": False})
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/fan-cards", methods=["GET", "POST"], endpoint="admin.fan_cards")
@admin_required
def admin_fan_cards():
    if request.method == "POST":
        pf_id = request.form.get("pf_id", "")
        name = request.form.get("name", "").strip()
        pf = User.query.get(int(pf_id)) if pf_id.isdigit() else None
        if not pf:
            flash("Please choose a public figure to assign this design to.", "error")
        elif not name:
            flash("Please enter a design name.", "error")
        else:
            preview_url = ""
            preview_file = request.files.get("preview")
            if preview_file and preview_file.filename:
                preview_url = save_upload(preview_file) or ""
            design = FanCardDesign(assigned_public_figure_id=pf.id, name=name, preview=preview_url, active=True)
            db.session.add(design)
            db.session.commit()
            flash("Design added.", "success")
    pfs = User.query.filter_by(role="public_figure").all()
    designs = FanCardDesign.query.order_by(FanCardDesign.id.desc()).all()
    return render_template("admin/fan_cards.html", pfs=pfs, designs=designs)


@app.route("/admin/fan-cards/<int:did>/assign", methods=["POST"], endpoint="admin.assign_design")
@admin_required
def admin_assign_design(did):
    design = FanCardDesign.query.get_or_404(did)
    pf_id = request.form.get("pf_id", "")
    design.assigned_public_figure_id = int(pf_id) if pf_id.isdigit() else None
    db.session.commit()
    flash("Design assignment updated.", "success")
    return redirect(url_for("admin.fan_cards"))


@app.route("/admin/fan-cards/<int:did>/toggle-active", methods=["POST"], endpoint="admin.toggle_design_active")
@admin_required
def admin_toggle_design_active(did):
    design = FanCardDesign.query.get_or_404(did)
    design.active = not design.active
    db.session.commit()
    return redirect(url_for("admin.fan_cards"))


@app.route("/admin/fan-cards/<int:did>/editor", endpoint="admin.design_editor")
@admin_required
def admin_design_editor(did):
    design = FanCardDesign.query.get_or_404(did)
    fields = {}
    try:
        fields = json.loads(design.design_data) if design.design_data else {}
    except Exception:
        fields = {}
    return render_template("admin/design_editor.html", design=design, fields=fields)


@app.route("/admin/fan-cards/<int:did>/editor/background", methods=["POST"], endpoint="admin.design_editor_bg")
@admin_required
def admin_design_editor_bg(did):
    design = FanCardDesign.query.get_or_404(did)
    bg = request.files.get("background")
    if not bg or not bg.filename:
        return jsonify({"ok": False, "error": "No image provided"}), 400
    url = save_upload(bg)
    if not url:
        return jsonify({"ok": False, "error": "Failed to save image"}), 400
    design.preview = url
    db.session.commit()
    return jsonify({"ok": True, "url": url})


@app.route("/admin/fan-cards/<int:did>/editor/save", methods=["POST"], endpoint="admin.design_editor_save")
@admin_required
def admin_design_editor_save(did):
    design = FanCardDesign.query.get_or_404(did)
    data = request.get_json(silent=True) or {}
    fields = data.get("fields", {})
    clean = {}
    for key in ("name", "photo", "expiry_date", "special_code"):
        if key in fields and isinstance(fields[key], dict):
            f = fields[key]
            clean[key] = {
                "x": float(f.get("x", 0)), "y": float(f.get("y", 0)),
                "w": float(f.get("w", 20)), "h": float(f.get("h", 8)),
                "fontPct": max(1, min(20, float(f.get("fontPct", 5)))),
                "color": str(f.get("color", "#ffffff"))[:20],
                "align": str(f.get("align", "left"))[:10],
                "enabled": bool(f.get("enabled", True)),
            }
    design.design_data = json.dumps(clean)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/fan-cards/<int:did>/delete", methods=["POST"], endpoint="admin.delete_design")
@admin_required
def admin_delete_design(did):
    design = FanCardDesign.query.get_or_404(did)
    db.session.delete(design)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/fan-card-requests", endpoint="admin.fan_card_requests")
@admin_required
def admin_fan_card_requests():
    cards = FanCard.query.filter_by(status="pending").order_by(FanCard.created_at.desc()).all()
    users = {u.id: u for u in User.query.all()}
    return render_template("admin/fan_card_requests.html", cards=cards, users=users)


@app.route("/admin/fan-card/<int:cid>/<action>", methods=["POST"], endpoint="admin.fan_card_action")
@admin_required
def admin_fan_card_action(cid, action):
    card = FanCard.query.get_or_404(cid)
    if action == "approve":
        card.status = "approved"
        image_url = card.generated_card or card.photo
        if image_url and card.public_figure_id:
            m = Message(sender_id=card.public_figure_id, receiver_id=card.user_id,
                        message="Your fan card was approved! 🎉", message_type="image",
                        media_url=image_url)
            db.session.add(m)
            db.session.commit()
            socketio.emit("message", {
                "id": m.id, "sender_id": card.public_figure_id, "receiver_id": card.user_id,
                "message": m.message, "type": "image", "media": image_url,
                "created_at": m.created_at.isoformat(), "read": False
            }, room="user_%d" % card.user_id)
            socketio.emit("message", {
                "id": m.id, "sender_id": card.public_figure_id, "receiver_id": card.user_id,
                "message": m.message, "type": "image", "media": image_url,
                "created_at": m.created_at.isoformat(), "read": False
            }, room="user_%d" % card.public_figure_id)
    elif action == "reject":
        card.status = "rejected"
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/voice-effects", endpoint="admin.voice_effects")
@admin_required
def admin_voice_effects():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            effect = VoiceEffect(name=name, active=True)
            db.session.add(effect)
            db.session.commit()
            flash("Voice effect added.", "success")
    effects = VoiceEffect.query.order_by(VoiceEffect.id.desc()).all()
    return render_template("admin/voice_effects.html", effects=effects)


@app.route("/admin/voice-effects/<int:eid>/toggle", methods=["POST"], endpoint="admin.toggle_voice")
@admin_required
def admin_toggle_voice(eid):
    e = VoiceEffect.query.get_or_404(eid)
    e.active = not e.active
    db.session.commit()
    return jsonify({"ok": True, "active": e.active})


@app.route("/admin/voice-effects/<int:eid>/delete", methods=["POST"], endpoint="admin.delete_voice")
@admin_required
def admin_delete_voice(eid):
    e = VoiceEffect.query.get_or_404(eid)
    db.session.delete(e)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/video-library", methods=["GET", "POST"], endpoint="admin.video_library")
@admin_required
def admin_video_library():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        url = request.form.get("url", "").strip()
        thumb = request.form.get("thumbnail", "").strip()
        if all([title, url]):
            vid = VideoLibrary(title=title, video_url=url, thumbnail=thumb, active=True)
            db.session.add(vid)
            db.session.commit()
            flash("Video added.", "success")
        return redirect(url_for("admin.video_library"))
    vids = VideoLibrary.query.order_by(VideoLibrary.id.desc()).all()
    return render_template("admin/video_library.html", vids=vids)


@app.route("/admin/video-library/<int:vid>/toggle", methods=["POST"], endpoint="admin.toggle_video")
@admin_required
def admin_toggle_video(vid):
    v = VideoLibrary.query.get_or_404(vid)
    v.active = not v.active
    db.session.commit()
    return jsonify({"ok": True, "active": v.active})


@app.route("/admin/video-library/<int:vid>/delete", methods=["POST"], endpoint="admin.delete_video")
@admin_required
def admin_delete_video(vid):
    v = VideoLibrary.query.get_or_404(vid)
    db.session.delete(v)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/followers-likes", methods=["GET", "POST"], endpoint="admin.followers_likes")
@admin_required
def admin_followers_likes():
    if request.method == "POST":
        pf_id = request.form.get("pf_id", "")
        followers = request.form.get("followers", "")
        likes = request.form.get("likes", "")
        pf = User.query.get(int(pf_id)) if pf_id.isdigit() else None
        if pf:
            if followers.lstrip("-").isdigit():
                pf.followers_count = int(followers)
            if likes.lstrip("-").isdigit():
                pf.likes_count = int(likes)
            db.session.commit()
            flash("Updated.", "success")
        else:
            flash("Invalid public figure selected.", "error")
        return redirect(url_for("admin.followers_likes"))
    pfs = User.query.filter_by(role="public_figure").all()
    return render_template("admin/followers_likes.html", pfs=pfs)


@app.route("/admin/messages", endpoint="admin.messages")
@admin_required
def admin_messages():
    msgs = Message.query.order_by(Message.created_at.desc()).limit(300).all()
    users = {u.id: u for u in User.query.all()}
    return render_template("admin/messages.html", messages=msgs, users=users)


@app.route("/admin/settings", methods=["GET", "POST"], endpoint="admin.settings")
@admin_required
def admin_settings():
    if request.method == "POST":
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html")


# ---------------- API ----------------
@app.route("/api/me", endpoint="api.me")
@login_required
def api_me():
    u = current_user()
    return jsonify({"id": u.id, "name": u.full_name, "role": u.role})


@app.route("/api/user/<int:uid>/details", endpoint="api.user_details")
@admin_required
def api_user_details(uid):
    """Admin API to get user details including country and device"""
    u = User.query.get_or_404(uid)
    device_info = {}
    if u.device_info:
        try:
            device_info = json.loads(u.device_info)
        except:
            device_info = {}
    
    return jsonify({
        "id": u.id,
        "name": u.full_name,
        "email": u.email,
        "phone": u.phone,
        "phone_verified": u.phone_verified,
        "country_code": u.country_code,
        "country_name": u.country_name,
        "ip_address": u.ip_address,
        "device_info": device_info,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "created_at": u.created_at.isoformat(),
        "role": u.role,
        "status": u.status
    })


@app.route("/api/messages/<int:peer_id>", endpoint="api.messages")
@login_required
def api_messages(peer_id):
    u = current_user()
    msgs = Message.query.filter(
        ((Message.sender_id == u.id) & (Message.receiver_id == peer_id)) |
        ((Message.sender_id == peer_id) & (Message.receiver_id == u.id))
    ).order_by(Message.created_at.asc()).limit(500).all()
    return jsonify([{"id": m.id, "sender_id": m.sender_id,
                     "receiver_id": m.receiver_id, "message": m.message,
                     "type": m.message_type, "media": m.media_url,
                     "read": m.read_status,
                     "created_at": m.created_at.isoformat()} for m in msgs])


@app.route("/api/upload", methods=["POST"], endpoint="api.upload")
@login_required
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    url = save_upload(f, ("image", "video"))
    if not url:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    return jsonify({"ok": True, "url": url})


@app.route("/api/voice-effects", endpoint="api.voice_effects")
@login_required
def api_voice_effects():
    effects = VoiceEffect.query.filter_by(active=True).all()
    return jsonify([e.name for e in effects])


@app.route("/api/video-library", endpoint="api.video_library")
@login_required
def api_video_library():
    vids = VideoLibrary.query.filter_by(active=True).all()
    return jsonify([{"id": v.id, "title": v.title, "url": v.video_url,
                     "thumbnail": v.thumbnail} for v in vids])


@app.route("/api/notifications/read", methods=["POST"], endpoint="api.notif_read")
@login_required
def api_notif_read():
    u = current_user()
    Notification.query.filter_by(user_id=u.id, read=False).update({"read": True})
    db.session.commit()
    return jsonify({"ok": True})


import re as _re
def extract_link_preview(url):
    """Fetch a title/thumbnail for a pasted TikTok or YouTube link via oEmbed."""
    try:
        import urllib.request, json as _json
        yt = _re.search(r'(youtube\.com/watch\?v=|youtu\.be/)([\w-]+)', url)
        tt = _re.search(r'tiktok\.com', url)
        if yt:
            oembed = "https://www.youtube.com/oembed?url=%s&format=json" % urllib.request.quote(url, safe="")
        elif tt:
            oembed = "https://www.tiktok.com/oembed?url=%s" % urllib.request.quote(url, safe="")
        else:
            return None
        with urllib.request.urlopen(oembed, timeout=5) as r:
            data = _json.loads(r.read().decode())
        return {"title": data.get("title", ""), "thumbnail": data.get("thumbnail_url", ""),
                "provider": "youtube" if yt else "tiktok", "url": url}
    except Exception:
        return None


@app.route("/api/link-preview", methods=["POST"])
@login_required
def api_link_preview():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400
    preview = extract_link_preview(url)
    if not preview:
        return jsonify({"ok": False, "error": "Only TikTok/YouTube links are supported"}), 400
    return jsonify({"ok": True, "preview": preview})


@app.route("/api/upload-voice", methods=["POST"], endpoint="api.upload_voice_note")
@login_required
def upload_voice_note():
    u = current_user()
    to = request.form.get("to")
    voice_file = request.files.get("voice")

    if not to or not str(to).isdigit() or not voice_file:
        return jsonify({"ok": False, "error": "Missing recipient or voice file"}), 400
    if int(to) not in _presence_audience(u.id):
        return jsonify({"ok": False, "error": "You can only message your connected public figure"}), 403

    if not allowed_file(voice_file.filename, kinds=("audio",)):
        return jsonify({"ok": False, "error": "Invalid audio file"}), 400

    url = save_upload(voice_file, kinds=("audio",))
    if not url:
        return jsonify({"ok": False, "error": "Failed to save voice note"}), 400

    m = Message(sender_id=u.id, receiver_id=int(to), message="",
                message_type="voice", media_url=url)
    db.session.add(m)
    db.session.commit()

    return jsonify({
        "ok": True,
        "message_id": m.id,
        "media_url": url,
        "created_at": m.created_at.isoformat()
    })


# ---------------- Socket.IO ----------------
# ---- Message safety ----
MAX_MESSAGE_LEN = 4000
MESSAGE_TYPES = {"text", "image", "video", "voice", "link"}
_SAFE_URL_RE = re.compile(r"^[A-Za-z0-9._~:/?#@!$&()*+,;=%\[\]-]+$")   # no quotes, <, >, spaces or backslashes
_LINK_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
               "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}


def is_own_media_url(url):
    """True only for files this app stored: a local upload or our own Cloudinary account."""
    if not isinstance(url, str) or not url or len(url) > 500 or not _SAFE_URL_RE.match(url):
        return False
    if url.startswith("/static/uploads/"):
        return ".." not in url
    if USE_CLOUDINARY:
        return url.startswith("https://res.cloudinary.com/%s/" % cloudinary.config().cloud_name)
    return False


def clean_link_preview(lp):
    """Rebuild a link preview from untrusted input: only TikTok/YouTube URLs, https thumbnails."""
    if not isinstance(lp, dict):
        return None
    url = lp.get("url")
    if not isinstance(url, str) or len(url) > 500 or not _SAFE_URL_RE.match(url):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in _LINK_HOSTS:
        return None
    thumb = lp.get("thumbnail") or ""
    if not (isinstance(thumb, str) and len(thumb) <= 500 and thumb.startswith("https://") and _SAFE_URL_RE.match(thumb)):
        thumb = ""
    provider = "tiktok" if "tiktok" in (parsed.hostname or "") else "youtube"
    return {"url": url, "title": str(lp.get("title") or "")[:200], "thumbnail": thumb, "provider": provider}


# ---- Presence: who is actually connected right now ----
# Single worker process (see Dockerfile), so in-memory tracking is enough.
_online = {}            # user_id -> set of connected socket ids
PRESENCE_GRACE = 4      # seconds; hides the brief reconnect when a page navigates


def _presence_audience(uid):
    """Users who should see this user's status: a fan sees their public figure,
    a public figure sees their fans."""
    u = User.query.get(uid)
    if not u:
        return []
    if u.role == "public_figure":
        return [r[0] for r in db.session.query(User.id).filter(User.assigned_public_figure_id == uid).all()]
    return [u.assigned_public_figure_id] if u.assigned_public_figure_id else []


def _broadcast_presence(uid, online):
    for rid in _presence_audience(uid):
        socketio.emit("presence", {"user_id": uid, "online": online}, room="user_%d" % rid)


def _mark_online(uid, sid):
    sids = _online.setdefault(uid, set())
    was_online = bool(sids)
    sids.add(sid)
    if not was_online:
        _broadcast_presence(uid, True)


def _delayed_offline(uid):
    socketio.sleep(PRESENCE_GRACE)
    if uid not in _online:  # no new connection arrived during the grace period
        with app.app_context():
            _broadcast_presence(uid, False)
            _drop_calls_for(uid)


def _mark_offline(uid, sid):
    sids = _online.get(uid)
    if not sids:
        return
    sids.discard(sid)
    if not sids:
        _online.pop(uid, None)
        socketio.start_background_task(_delayed_offline, uid)


def is_user_online(uid):
    return bool(_online.get(uid))


@socketio.on("presence:get")
def on_presence_get(data):
    uid = session.get("user_id")
    target = (data or {}).get("id")
    if not uid or not str(target).isdigit():
        return {"online": False}
    target = int(target)
    if target not in _presence_audience(uid):
        return {"online": False}
    return {"online": is_user_online(target)}


@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if uid:
        join_room("user_%d" % uid)
        _mark_online(uid, request.sid)


@socketio.on("join")
def on_join(data=None):
    uid = session.get("user_id")
    if uid:
        join_room("user_%d" % uid)


@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    to = (data or {}).get("to")
    if uid and to and str(to).isdigit() and int(to) in _presence_audience(uid):
        emit("typing", {"from": uid}, room="user_%d" % int(to))


@socketio.on("message")
def on_message(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    raw_to = data.get("to")
    if raw_to is None or not str(raw_to).isdigit():
        return
    to = int(raw_to)
    # Only a fan and the public figure they're connected to may message each other
    if to not in _presence_audience(uid):
        emit("message:error", {"reason": "not_allowed"})
        return

    mtype = data.get("type", "text")
    if mtype not in MESSAGE_TYPES:
        emit("message:error", {"reason": "invalid"})
        return
    text = data.get("message", "")
    text = text.strip()[:MAX_MESSAGE_LEN] if isinstance(text, str) else ""
    media = data.get("media", "")

    if mtype == "link":
        lp = clean_link_preview(data.get("link_preview"))
        if not lp:
            emit("message:error", {"reason": "invalid"})
            return
        text, media = json.dumps(lp), ""
    elif mtype in ("image", "video", "voice"):
        if not is_own_media_url(media):
            emit("message:error", {"reason": "invalid"})
            return
    else:  # plain text
        media = ""
        if not text:
            return

    m = Message(sender_id=uid, receiver_id=to, message=text,
                message_type=mtype, media_url=media)
    db.session.add(m)
    db.session.commit()
    payload = {"id": m.id, "sender_id": uid, "receiver_id": to,
               "message": text, "type": mtype, "media": media,
               "created_at": m.created_at.isoformat(), "read": False}
    emit("message", payload, room="user_%d" % to)
    emit("message", payload, room="user_%d" % uid)


@socketio.on("read")
def on_read(data):
    uid = session.get("user_id")
    peer = (data or {}).get("peer")
    if uid and peer and str(peer).isdigit() and int(peer) in _presence_audience(uid):
        Message.query.filter_by(sender_id=int(peer), receiver_id=uid, read_status=False)\
                      .update({"read_status": True})
        db.session.commit()
        emit("read", {"by": uid}, room="user_%d" % int(peer))


_active_calls = {}      # (caller_id, receiver_id) -> Call.id for the ringing/connected call between a pair
_call_answered_at = {}  # Call.id -> when it was picked up, so duration excludes ringing time


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")
    if not uid:
        return
    # Presence + any call cleanup happens after a short grace period (see _delayed_offline),
    # and only if the user has no other tab/connection left.
    _mark_offline(uid, request.sid)


def _may_signal(uid, to_id):
    """Calls are only allowed between a fan and the public figure they're connected to."""
    if (uid, to_id) in _active_calls or (to_id, uid) in _active_calls:
        return True
    return to_id in _presence_audience(uid)


def _finish_call(call_pk, ringing_status):
    """Close out a Call row: a ringing call becomes `ringing_status`; a connected one gets
    its end time and talk duration."""
    call = Call.query.get(call_pk)
    if not call:
        _call_answered_at.pop(call_pk, None)
        return None
    now = datetime.utcnow()
    if call.status == "ringing":
        call.status = ringing_status
        call.ended_at = now
    elif call.status == "completed" and not call.ended_at:
        call.ended_at = now
        started = _call_answered_at.get(call_pk) or call.created_at
        call.duration = max(0, int((now - started).total_seconds()))
    db.session.commit()
    _call_answered_at.pop(call_pk, None)
    return call


def _drop_calls_for(uid):
    """The user is completely gone (no connection left): end their calls and tell the other side."""
    for key in [k for k in _active_calls if uid in k]:
        call_pk = _active_calls.pop(key, None)
        if call_pk:
            _finish_call(call_pk, "failed")
        other = key[0] if key[1] == uid else key[1]
        socketio.emit("call:end", {"from": uid}, room="user_%d" % other)


def _ring_timeout(call_pk, caller_id, receiver_id):
    socketio.sleep(app.config["CALL_RING_TIMEOUT"])
    with app.app_context():
        call = Call.query.get(call_pk)
        if not call or call.status != "ringing":
            return
        if _active_calls.get((caller_id, receiver_id)) == call_pk:
            _active_calls.pop((caller_id, receiver_id), None)
        _finish_call(call_pk, "missed")
        socketio.emit("call:end", {"from": receiver_id}, room="user_%d" % caller_id)
        socketio.emit("call:end", {"from": caller_id}, room="user_%d" % receiver_id)


def _record_missed(caller_id, receiver_id, call_type):
    """Log a call that could not ring (so the public figure still sees who tried)."""
    now = datetime.utcnow()
    db.session.add(Call(caller_id=caller_id, receiver_id=receiver_id, call_type=call_type,
                        status="missed", ended_at=now))
    db.session.commit()


@socketio.on("call:offer")
def call_offer(data):
    uid = session.get("user_id")
    to = (data or {}).get("to")
    if not uid or not to or not str(to).isdigit():
        return
    to_id = int(to)
    ctype = data.get("type", "voice")
    if ctype not in ("voice", "video"):
        ctype = "voice"

    if to_id == uid or to_id not in _presence_audience(uid):
        emit("call:unavailable", {"to": to_id, "reason": "not_allowed"})
        return
    if not is_user_online(to_id):
        _record_missed(uid, to_id, ctype)
        emit("call:unavailable", {"to": to_id, "reason": "offline"})
        return
    if any(to_id in k and uid not in k for k in _active_calls):
        _record_missed(uid, to_id, ctype)
        emit("call:unavailable", {"to": to_id, "reason": "busy"})
        return

    # A fresh call from the same caller replaces any earlier unfinished one
    old = _active_calls.pop((uid, to_id), None)
    if old:
        _finish_call(old, "missed")

    call = Call(caller_id=uid, receiver_id=to_id, call_type=ctype, status="ringing")
    db.session.add(call)
    db.session.commit()
    _active_calls[(uid, to_id)] = call.id
    socketio.start_background_task(_ring_timeout, call.id, uid, to_id)
    emit("call:offer", {"from": uid, "sdp": data.get("sdp"), "type": ctype,
                        "name": data.get("name", ""), "call_id": call.id},
         room="user_%d" % to_id)


@socketio.on("call:answer")
def call_answer(data):
    uid = session.get("user_id")
    to = (data or {}).get("to")
    if not uid or not to or not str(to).isdigit():
        return
    to_id = int(to)
    call_pk = _active_calls.get((to_id, uid))  # the caller was `to_id`; we (uid) are answering
    if not call_pk:
        # Nothing to answer any more (caller hung up or it timed out): tell this client to stop
        emit("call:end", {"from": to_id})
        return
    call = Call.query.get(call_pk)
    if call and call.status == "ringing":
        call.status = "completed"
        db.session.commit()
        _call_answered_at[call_pk] = datetime.utcnow()
    payload = {"from": uid, "sdp": data.get("sdp")}
    if data.get("premade"):
        # Never trust the client's claimed video; always re-verify against the DB
        pf = User.query.get(uid)
        if pf and pf.is_public_figure and pf.call_video_url:
            payload["premade"] = True
            payload["video_url"] = pf.call_video_url
            if pf.mouth_x is not None and pf.mouth_y is not None:
                payload["mouth_x"] = pf.mouth_x
                payload["mouth_y"] = pf.mouth_y
    emit("call:answer", payload, room="user_%d" % to_id)


@socketio.on("call:ice")
def call_ice(data):
    uid = session.get("user_id")
    to = (data or {}).get("to")
    if uid and to and str(to).isdigit() and _may_signal(uid, int(to)):
        emit("call:ice", {"from": uid, "candidate": data.get("candidate")}, room="user_%d" % int(to))


@socketio.on("call:end")
def call_end(data):
    uid = session.get("user_id")
    to = (data or {}).get("to")
    if not uid or not to or not str(to).isdigit():
        return
    to_id = int(to)
    if not _may_signal(uid, to_id):
        return
    key = (uid, to_id) if (uid, to_id) in _active_calls else ((to_id, uid) if (to_id, uid) in _active_calls else None)
    if key:
        call_pk = _active_calls.pop(key)
        # still ringing: the receiver hanging up = declined, the caller hanging up = missed
        _finish_call(call_pk, "declined" if key[1] == uid else "missed")
    emit("call:end", {"from": uid}, room="user_%d" % to_id)


# ---------------- Lip Sync ----------------
@app.route("/lip-sync/dashboard", endpoint="lip_sync.dashboard")
@public_figure_required
def lip_sync_dashboard():
    """Public figure lip sync dashboard"""
    pf = current_user()
    sessions = []
    # Get recent sessions from database or cache
    return render_template("lip_sync_dashboard.html", public_figure=pf, sessions=sessions)


@app.route("/lip-sync/session/<session_id>", endpoint="lip_sync.session")
@public_figure_required
def lip_sync_session(session_id):
    """Lip sync video processing session"""
    pf = current_user()
    return render_template("lip_sync_session.html", public_figure=pf, session_id=session_id)


@app.route("/api/lip-sync/start", methods=["POST"], endpoint="api.lip_sync_start")
@public_figure_required
def api_lip_sync_start():
    """Start a new lip sync session"""
    pf = current_user()
    session_id = "%d_%d" % (pf.id, int(datetime.utcnow().timestamp()))
    lip_sync_sessions[session_id] = SessionRecorder()
    return jsonify({"ok": True, "session_id": session_id})


@app.route("/api/lip-sync/end", methods=["POST"], endpoint="api.lip_sync_end")
@public_figure_required
def api_lip_sync_end():
    """End lip sync session and generate video"""
    pf = current_user()
    session_id = request.form.get("session_id")
    video_file = request.files.get("video")
    
    if not session_id or session_id not in lip_sync_sessions:
        return jsonify({"ok": False, "error": "Invalid session"}), 400
    
    if not video_file:
        return jsonify({"ok": False, "error": "No video provided"}), 400
    
    if not video_generator:
        return jsonify({"ok": False, "error": "Lip sync processing is not available"}), 503

    src_path = output_path = None
    try:
        # The video processor needs real files on disk, so work locally and publish the result.
        src_url = save_local(video_file, ("video",))
        if not src_url:
            return jsonify({"ok": False, "error": "Failed to save video"}), 400
        src_path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(src_url))

        recorder = lip_sync_sessions[session_id]
        stats = recorder.get_session_stats()

        timestamp = int(datetime.utcnow().timestamp())
        output_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{timestamp}_lipsync_output.mp4")
        video_generator.process_video_with_realtime_data(src_path, recorder.frames, output_path)

        del lip_sync_sessions[session_id]

        output_url = publish_local_file(output_path)
        if not output_url:
            return jsonify({"ok": False, "error": "Could not store the generated video"}), 500

        return jsonify({
            "ok": True,
            "video_url": output_url,
            "stats": stats,
            "message": "Lip sync video generated successfully"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        for tmp in (src_path, output_path):
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


@app.route("/api/lip-sync/frame", methods=["POST"], endpoint="api.lip_sync_frame")
@public_figure_required
def api_lip_sync_frame():
    """Add a camera frame to lip sync session"""
    pf = current_user()
    session_id = request.form.get("session_id")
    frame_data = request.form.get("frame_data")
    landmarks = request.form.get("landmarks")
    
    if not session_id or session_id not in lip_sync_sessions:
        return jsonify({"ok": False, "error": "Invalid session"}), 400
    
    try:
        recorder = lip_sync_sessions[session_id]
        # Add frame data to recorder
        timestamp = float(request.form.get("timestamp", 0))
        recorder.add_frame({
            "data": frame_data,
            "landmarks": json.loads(landmarks) if landmarks else []
        }, timestamp)
        
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@socketio.on("lip_sync:frame")
def on_lip_sync_frame(data):
    """Handle real-time lip sync frame data via WebSocket"""
    uid = session.get("user_id")
    if not uid:
        return
    
    session_id = data.get("session_id")
    if session_id and session_id in lip_sync_sessions:
        try:
            recorder = lip_sync_sessions[session_id]
            timestamp = data.get("timestamp", 0)
            frame_data = data.get("frame_data")
            landmarks = data.get("landmarks", [])
            
            recorder.add_frame({
                "data": frame_data,
                "landmarks": landmarks
            }, timestamp)
            
            emit("lip_sync:frame_ack", {"ok": True})
        except:
            emit("lip_sync:frame_ack", {"ok": False})


@socketio.on("lip_sync:start")
def on_lip_sync_start(data):
    """Start lip sync session"""
    uid = session.get("user_id")
    if not uid:
        return
    
    u = User.query.get(uid)
    if not u or not u.is_public_figure:
        return
    
    session_id = "%d_%d" % (uid, int(datetime.utcnow().timestamp()))
    lip_sync_sessions[session_id] = SessionRecorder()
    emit("lip_sync:session_started", {"session_id": session_id})


# ---------------- Init ----------------
with app.app_context():
    init_db(app)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
