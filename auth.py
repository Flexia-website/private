from functools import wraps
from flask import session, redirect, url_for, request, jsonify


def current_user():
    from models import User
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id") or not current_user():
            session.pop("user_id", None)
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "auth required"}), 401
                return redirect(url_for("auth.login"))
            if u.role not in roles:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "forbidden"}), 403
                return redirect(url_for("home_router"))
            return f(*args, **kwargs)
        return wrapper
    return deco


def admin_required(f):
    return role_required("admin")(f)


def public_figure_required(f):
    return role_required("public_figure")(f)


def user_required(f):
    return role_required("user")(f)
