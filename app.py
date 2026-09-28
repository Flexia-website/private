import os
import json
import random
import base64
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename
import phonenumbers
from config import Config
from models import (db, User, Connection, Message, Call, FanCard,
                    FanCardDesign, VoiceEffect, VideoLibrary, Notification)
from database import init_db
from auth import (current_user, login_required, admin_required,
                  public_figure_required, user_required)
from lip_sync import LipSyncProcessor
from lip_sync_video_processor import LipSyncVideoGenerator, SessionRecorder

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config.from_object(Config)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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


def save_upload(file, kinds=("image",)):
    if not file or file.filename == "":
        return ""
    if not allowed_file(file.filename, kinds):
        return ""
    fname = "%d_%s" % (int(datetime.utcnow().timestamp()), secure_filename(file.filename))
    path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
    file.save(path)
    return "/static/uploads/" + fname


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


@app.context_processor
def inject_globals():
    return {"current_user": current_user(), "now": datetime.utcnow()}


app.jinja_env.filters["humanize"] = humanize_count


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
        phone = request.form.get("phone", "").strip()
        pf = User.query.filter_by(phone=phone, role="public_figure", status="active").first()
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
    return render_template("user/chat.html", user=u, peer=pf, messages=msgs)


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
        msg = request.form.get("message", "").strip()
        design = None
        if design_id.isdigit():
            design = FanCardDesign.query.get(int(design_id))
        if not design:
            flash("Invalid design.", "error")
        else:
            card = FanCard(user_id=u.id, public_figure_id=pf.id, design_id=design.id, name=msg)
            db.session.add(card)
            db.session.commit()
            flash("Fan card created! Awaiting approval.", "info")
            return redirect(url_for("user.profile"))
    designs = FanCardDesign.query.filter_by(public_figure_id=u.assigned_public_figure_id, active=True).all()
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
            db.session.commit()
            return jsonify({"ok": True, "video_url": url})
        return jsonify({"ok": False, "error": "No video provided."}), 400
    return render_template("public/call_video.html", pf=pf)


@app.route("/public/call-video/remove", methods=["POST"], endpoint="public.call_video_remove")
@public_figure_required
def public_call_video_remove():
    pf = current_user()
    pf.call_video_url = ""
    db.session.commit()
    return jsonify({"ok": True})


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
    if u and pf:
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
            flash("Invalid public figure.", "error")
        else:
            design = FanCardDesign(public_figure_id=pf.id, name=name, active=True)
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
    design.active = not design.active
    db.session.commit()
    return jsonify({"ok": True, "active": design.active})


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


@app.route("/api/upload-voice", methods=["POST"], endpoint="api.upload_voice_note")
@login_required
def upload_voice_note():
    u = current_user()
    to = request.form.get("to")
    voice_file = request.files.get("voice")

    if not to or not voice_file:
        return jsonify({"ok": False, "error": "Missing recipient or voice file"}), 400

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
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if uid:
        join_room("user_%d" % uid)


@socketio.on("join")
def on_join(data=None):
    uid = session.get("user_id")
    if uid:
        join_room("user_%d" % uid)


@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    to = data.get("to")
    if uid and to and str(to).isdigit():
        emit("typing", {"from": uid}, room="user_%d" % int(to))


@socketio.on("message")
def on_message(data):
    uid = session.get("user_id")
    if not uid:
        return
    raw_to = data.get("to")
    if raw_to is None or not str(raw_to).isdigit():
        return
    to = int(raw_to)
    text = data.get("message", "")
    mtype = data.get("type", "text")
    media = data.get("media", "")
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
    peer = data.get("peer")
    if uid and peer and str(peer).isdigit():
        Message.query.filter_by(sender_id=int(peer), receiver_id=uid, read_status=False)\
                      .update({"read_status": True})
        db.session.commit()
        emit("read", {"by": uid}, room="user_%d" % int(peer))


@socketio.on("call:offer")
def call_offer(data):
    uid = session.get("user_id")
    to = data.get("to")
    if uid and to and str(to).isdigit():
        emit("call:offer", {"from": uid, "sdp": data.get("sdp"),
                            "type": data.get("type", "voice"),
                            "name": data.get("name", "")},
             room="user_%d" % int(to))


@socketio.on("call:answer")
def call_answer(data):
    uid = session.get("user_id")
    to = data.get("to")
    if not uid or not to or not str(to).isdigit():
        return
    payload = {"from": uid, "sdp": data.get("sdp")}
    if data.get("premade"):
        # Never trust the client's claimed video; always re-verify against the DB
        pf = User.query.get(uid)
        if pf and pf.is_public_figure and pf.call_video_url:
            payload["premade"] = True
            payload["video_url"] = pf.call_video_url
    emit("call:answer", payload, room="user_%d" % int(to))


@socketio.on("call:ice")
def call_ice(data):
    uid = session.get("user_id")
    to = data.get("to")
    if uid and to and str(to).isdigit():
        emit("call:ice", {"from": uid, "candidate": data.get("candidate")}, room="user_%d" % int(to))


@socketio.on("call:end")
def call_end(data):
    uid = session.get("user_id")
    to = data.get("to")
    if uid and to and str(to).isdigit():
        emit("call:end", {"from": uid}, room="user_%d" % int(to))


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
    
    try:
        # Save original video
        video_path = save_upload(video_file, ("video",))
        if not video_path:
            return jsonify({"ok": False, "error": "Failed to save video"}), 400
        
        # Process with lip sync
        recorder = lip_sync_sessions[session_id]
        stats = recorder.get_session_stats()
        
        # Generate output path
        timestamp = int(datetime.utcnow().timestamp())
        output_filename = f"{timestamp}_lipsync_output.mp4"
        output_path = os.path.join(app.config["UPLOAD_FOLDER"], output_filename)
        output_url = "/static/uploads/" + output_filename
        
        # Process video with lip sync
        if video_generator:
            video_generator.process_video_with_realtime_data(
                os.path.join("static", video_path.lstrip("/")),
                recorder.frames,
                output_path
            )
        
        # Clean up session
        del lip_sync_sessions[session_id]
        
        return jsonify({
            "ok": True,
            "video_url": output_url,
            "stats": stats,
            "message": "Lip sync video generated successfully"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
