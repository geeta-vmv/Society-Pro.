"""
SocietyPro Backend  –  Complete RBAC implementation
Roles: SOCIETY_ADMIN | OWNER | TENANT | GUARD
Deps : flask, PyJWT, PyYAML  (all available by default in image)
"""
import os, re, time, random, string, sqlite3, logging
from datetime import datetime, timedelta, date
from functools import wraps
from decimal import Decimal

from flask import Flask, request, jsonify, g, send_from_directory
import jwt as pyjwt
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("societypro")

_HERE     = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.environ.get("CONFIG_PATH", os.path.join(_HERE, "..", "config.yaml"))

def _load_cfg():
    with open(_CFG_PATH) as f: c = yaml.safe_load(f)
    c["app"]["secret_key"]             = os.environ.get("SECRET_KEY",      c["app"]["secret_key"])
    c["otp"]["msg91"]["auth_key"]      = os.environ.get("MSG91_AUTH_KEY",  c["otp"]["msg91"]["auth_key"])
    c["payment"]["razorpay"]["key_id"] = os.environ.get("RAZORPAY_KEY_ID", c["payment"]["razorpay"]["key_id"])
    c["payment"]["razorpay"]["key_secret"] = os.environ.get("RAZORPAY_SECRET", c["payment"]["razorpay"].get("key_secret",""))
    return c

CFG    = _load_cfg()
STATIC = os.path.join(_HERE, "static")
app    = Flask(__name__, static_folder=STATIC, static_url_path="")
app.config["SECRET_KEY"] = CFG["app"]["secret_key"]

DB_PATH = os.environ.get("DB_PATH", os.path.join(_HERE, "societypro.db"))

# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def q(sql, params=(), one=False, commit=False):
    db = get_db()
    cur = db.execute(sql, params)
    if commit: db.commit(); return cur.lastrowid
    return cur.fetchone() if one else cur.fetchall()

def qall(s,p=()): return q(s,p)
def qone(s,p=()): return q(s,p,one=True)
def qrun(s,p=()): return q(s,p,commit=True)
def r2d(r): return dict(r) if r else None
def r2l(rows): return [dict(r) for r in (rows or [])]

# ── OTP store ──────────────────────────────────────────────────────────────────
_OTP = {}
def otp_set(m,o): _OTP[m]={"otp":o,"exp":time.time()+CFG["otp"]["expiry_minutes"]*60}
def otp_get(m):
    r=_OTP.get(m); return r["otp"] if r and time.time()<r["exp"] else None
def otp_del(m): _OTP.pop(m,None)
def gen_otp(): return "".join(random.choices(string.digits,k=CFG["otp"]["length"]))
def gen_receipt(sid):
    return f"RCP-{sid}-{datetime.now().strftime('%Y%m%d')}-{''.join(random.choices(string.digits,k=6))}"

def send_otp_sms(mobile, otp):
    log.info(f"\n{'='*52}\n  📱  OTP for {mobile}:  {otp}\n{'='*52}")
    if CFG["otp"]["provider"] == "msg91":
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({
                "authkey": CFG["otp"]["msg91"]["auth_key"],
                "mobiles": f"91{mobile}", "message": f"SocietyPro OTP: {otp}. Valid 5 min.",
                "sender": "SCPRO", "route": "4"}).encode()
            urllib.request.urlopen("https://api.msg91.com/api/sendhttp.php", data, timeout=5)
        except Exception as e: log.warning(f"MSG91: {e}")
    elif CFG["otp"]["provider"] == "fast2sms":
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": CFG["otp"]["fast2sms"]["api_key"],
                         "Content-Type": "application/json"})
            import json
            data = json.dumps({"route":"q","message":f"SocietyPro OTP: {otp}","language":"english","flash":0,"numbers":mobile}).encode()
            urllib.request.urlopen(req, data, timeout=5)
        except Exception as e: log.warning(f"fast2sms: {e}")

# ── JWT ────────────────────────────────────────────────────────────────────────
SECRET  = CFG["app"]["secret_key"]
JWT_EXP = CFG["jwt"]["access_token_expiry_hours"]

def make_token(uid, role):
    return pyjwt.encode(
        {"sub": uid, "role": role, "exp": datetime.utcnow() + timedelta(hours=JWT_EXP)},
        SECRET, algorithm="HS256")

# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION PLAN SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

# ── Feature registry — add new premium features here only ─────────────────────
PLAN_FEATURES = {
    # FREE features — available to all societies
    "FREE": [
        "flats",           # flat & resident management
        "visitors",        # visitor logging & approval
        "announcements",   # society announcements
        "feedback",        # complaints & feedback
        "guards",          # guard management
        "dashboard_basic", # basic dashboard
        "profile",         # user profile
    ],
    # PAID features — requires PAID plan approval
    "PAID": [
        "maintenance",       # maintenance bill generation
        "payments",          # payment collection & receipts
        "bank_account",      # bank account management
        "payment_history",   # payment history & receipts
        "dashboard_full",    # full stats + chart
        "service_directory", # service provider directory (electrician, plumber etc.)
        "service_requests",  # raise & track service appointments
        "reports",           # future: financial reports
        "bulk_sms",          # future: bulk SMS to residents
        "custom_branding",   # future: society branding
        "api_access",        # future: API access for integrations
    ],
}

# All features available to PAID plan
ALL_PAID_FEATURES = PLAN_FEATURES["FREE"] + PLAN_FEATURES["PAID"]

def _get_society_plan(sid):
    """Return current active plan for a society. Defaults to FREE."""
    row = qone("SELECT * FROM society_plans WHERE society_id=?", (sid,))
    if not row:
        # Auto-create FREE plan entry if missing
        qrun("INSERT OR IGNORE INTO society_plans(society_id,current_plan,request_status,created_at,updated_at) VALUES(?,'FREE','NONE',?,?)",
             (sid, _now(), _now()))
        return {"society_id": sid, "current_plan": "FREE", "request_status": "NONE",
                "requested_plan": None, "review_note": None}
    return r2d(row)

def _society_can(sid, feature):
    """Check if society has access to a given feature."""
    plan = _get_society_plan(sid)
    current = plan["current_plan"]
    if current == "PAID":
        return feature in ALL_PAID_FEATURES
    return feature in PLAN_FEATURES["FREE"]

def plan_required(feature):
    """Decorator: block endpoint if society doesn't have the feature."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # SUPER_ADMIN bypasses all plan checks
            if hasattr(g, 'role') and g.role == "SUPER_ADMIN":
                return f(*args, **kwargs)
            sid = (request.args.get("society_id")
                   or (request.json or {}).get("society_id")
                   or _get_user_society(g.uid))
            try: sid = int(sid)
            except: sid = 1
            if not _society_can(sid, feature):
                plan = _get_society_plan(sid)
                return err(
                    f"This feature requires a PAID plan. "
                    f"Your society is on the {plan['current_plan']} plan. "
                    f"Please request an upgrade from Society Admin.",
                    402  # Payment Required
                )
            return f(*args, **kwargs)
        return wrapper
    return decorator

def auth_required(f):
    @wraps(f)
    def w(*a, **kw):
        h = request.headers.get("Authorization", "")
        if not h.startswith("Bearer "): return err("Missing token", 401)
        try:
            p = pyjwt.decode(h[7:], SECRET, algorithms=["HS256"])
            g.uid = p["sub"]; g.role = p["role"]
        except pyjwt.ExpiredSignatureError: return err("Token expired", 401)
        except Exception: return err("Invalid token", 401)
        return f(*a, **kw)
    return w

def admin_only(f):
    @wraps(f)
    @auth_required
    def w(*a, **kw):
        if g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN"): return err("Admin access required", 403)
        return f(*a, **kw)
    return w

def staff_only(f):
    @wraps(f)
    @auth_required
    def w(*a, **kw):
        if g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN", "MANAGER"): return err("Staff access required", 403)
        return f(*a, **kw)
    return w

def super_only(f):
    """Decorator: allow only SUPER_ADMIN role."""
    @wraps(f)
    @auth_required
    def w(*a, **kw):
        if g.role != "SUPER_ADMIN": return err("Super admin access required", 403)
        return f(*a, **kw)
    return w

# ── Response helpers ───────────────────────────────────────────────────────────
def ok(data=None, msg="success", meta=None):
    r = {"success": True, "message": msg, "data": data}
    if meta: r["meta"] = meta
    return jsonify(r)

def err(msg, code=400):
    return jsonify({"success": False, "message": msg, "data": None}), code

def paginate(lst, page, per=20):
    t = len(lst); s = (page-1)*per
    return lst[s:s+per], {"page": page, "per_page": per, "total": t, "pages": (t+per-1)//per or 1}

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,PATCH,OPTIONS"
    return r

@app.route("/api/<path:p>", methods=["OPTIONS"])
def options(p): return ok()

def _now(): return datetime.utcnow().isoformat(sep=" ")

def _upsert_user(mobile, name, email, role):
    u = qone("SELECT id,role FROM users WHERE mobile=?", (mobile,))
    if u:
        if name: qrun("UPDATE users SET name=?,updated_at=? WHERE id=?", (name, _now(), u["id"]))
        return u["id"]
    return qrun(
        "INSERT INTO users(mobile,name,email,role,is_active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
        (mobile, name, email, role, _now(), _now()))

def _mask_bank(b):
    if not b: return None
    an = b.get("account_number","")
    b["account_number_masked"] = ("•"*(len(an)-4)+an[-4:]) if len(an)>4 else "••••"
    b.pop("account_number", None)
    return b

def _get_user_flat(uid):
    """Get the flat associated with this user (owner or tenant)"""
    flat = qone(
        "SELECT f.*,w.name wing_name FROM flats f "
        "LEFT JOIN wings w ON w.id=f.wing_id "
        "WHERE f.id=(SELECT flat_id FROM flat_owners WHERE user_id=? AND is_active=1 LIMIT 1)",
        (uid,))
    if not flat:
        flat = qone(
            "SELECT f.*,w.name wing_name FROM flats f "
            "LEFT JOIN wings w ON w.id=f.wing_id "
            "WHERE f.id=(SELECT flat_id FROM tenants WHERE user_id=? AND is_active=1 LIMIT 1)",
            (uid,))
    return r2d(flat)

def _get_user_society(uid):
    """Get society_id for this user"""
    us = qone("SELECT society_id FROM user_societies WHERE user_id=? AND is_active=1 LIMIT 1", (uid,))
    if us: return us["society_id"]
    # Fall back: from flat
    flat = _get_user_flat(uid)
    if flat: return flat["society_id"]
    return 1

# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/send-otp", methods=["POST"])
def send_otp_route():
    d = request.json or {}
    mobile = re.sub(r"\D", "", str(d.get("mobile", "")))
    if len(mobile) < 10: return err("Enter a valid 10-digit mobile number")
    otp = gen_otp()
    otp_set(mobile, otp)
    send_otp_sms(mobile, otp)
    return ok({"message": "OTP sent", "dev_otp": otp})   # dev_otp for testing

@app.route("/api/auth/verify-otp", methods=["POST"])
def verify_otp():
    d = request.json or {}
    mobile = re.sub(r"\D", "", str(d.get("mobile", "")))
    otp    = str(d.get("otp", "")).strip()
    stored = otp_get(mobile)
    if not stored or stored != otp: return err("Invalid or expired OTP")
    otp_del(mobile)
    user = qone("SELECT * FROM users WHERE mobile=?", (mobile,))
    if not user:
        uid  = qrun("INSERT INTO users(mobile,role,is_active,created_at,updated_at) VALUES(?,?,1,?,?)",
                    (mobile, "OWNER", _now(), _now()))
        user = qone("SELECT * FROM users WHERE id=?", (uid,))
    u = r2d(user)
    # Enrich with flat/society info
    u["flat"]      = _get_user_flat(u["id"])
    u["society_id"]= _get_user_society(u["id"])
    u["societies"] = r2l(qall(
        "SELECT s.id,s.name,us.role FROM user_societies us "
        "JOIN societies s ON s.id=us.society_id "
        "WHERE us.user_id=? AND us.is_active=1", (u["id"],)))
    return ok({"token": make_token(u["id"], u["role"]), "user": u})

@app.route("/api/auth/me", methods=["GET"])
@auth_required
def get_me():
    u = r2d(qone("SELECT * FROM users WHERE id=?", (g.uid,)))
    if not u: return err("User not found", 404)
    u["flat"]       = _get_user_flat(g.uid)
    u["society_id"] = _get_user_society(g.uid)
    u["societies"]  = r2l(qall(
        "SELECT s.id,s.name,us.role FROM user_societies us "
        "JOIN societies s ON s.id=us.society_id "
        "WHERE us.user_id=? AND us.is_active=1", (g.uid,)))
    # Guard info
    if u["role"] == "GUARD":
        guard = r2d(qone("SELECT * FROM guards WHERE user_id=? AND is_active=1", (g.uid,)))
        u["guard_info"] = guard
        if guard: u["society_id"] = guard["society_id"]
    return ok(u)

@app.route("/api/auth/profile", methods=["PUT"])
@auth_required
def update_profile():
    d = request.json or {}
    fields=[]; vals=[]
    if "name"      in d: fields.append("name=?");      vals.append(d["name"])
    if "email"     in d: fields.append("email=?");     vals.append(d["email"])
    if "fcm_token" in d: fields.append("fcm_token=?"); vals.append(d["fcm_token"])
    if fields:
        vals.extend([_now(), g.uid])
        qrun(f"UPDATE users SET {','.join(fields)},updated_at=? WHERE id=?", vals)
    return ok(r2d(qone("SELECT * FROM users WHERE id=?", (g.uid,))))

# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD  (role-aware)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/dashboard/stats", methods=["GET"])
@auth_required
def dashboard_stats():
    sid = int(request.args.get("society_id", _get_user_society(g.uid)))
    today_s  = date.today().isoformat()
    month_s  = date.today().replace(day=1).isoformat()

    if g.role in ("SOCIETY_ADMIN", "SUPER_ADMIN", "MANAGER"):
        tf  = qone("SELECT COUNT(*) c FROM flats WHERE society_id=? AND is_active=1",(sid,))["c"]
        occ = qone("SELECT COUNT(*) c FROM flats WHERE society_id=? AND is_active=1 AND status!='VACANT'",(sid,))["c"]
        col = float(qone("SELECT COALESCE(SUM(total_amount),0) s FROM payments WHERE society_id=? AND status='SUCCESS' AND period_start>=?",(sid,month_s))["s"])
        pb  = qone("SELECT COUNT(*) c FROM maintenance_bills WHERE society_id=? AND status='PENDING' AND bill_month=?",(sid,month_s))["c"]
        ob  = qone("SELECT COUNT(*) c FROM maintenance_bills WHERE society_id=? AND status='OVERDUE' AND bill_month=?",(sid,month_s))["c"]
        oc  = qone("SELECT COUNT(*) c FROM feedbacks WHERE society_id=? AND status='OPEN'",(sid,))["c"]
        vt  = qone("SELECT COUNT(*) c FROM visitor_logs WHERE society_id=? AND DATE(created_at)=?",(sid,today_s))["c"]
        pv  = qone("SELECT COUNT(*) c FROM visitor_logs WHERE society_id=? AND status='PENDING'",(sid,))["c"]
        chart=[]
        today=date.today()
        for i in range(5,-1,-1):
            mo=date(today.year,today.month,1)
            for _ in range(i): mo=(mo-timedelta(days=1)).replace(day=1)
            amt=float(qone("SELECT COALESCE(SUM(total_amount),0) s FROM payments WHERE society_id=? AND status='SUCCESS' AND period_start LIKE ?",(sid,f"{mo.strftime('%Y-%m')}%"))["s"])
            chart.append({"month":mo.strftime("%b"),"collected":amt})
        return ok({"role":"admin","total_flats":tf,"occupied":occ,"vacant":tf-occ,
                   "collected":col,"pending_bills":pb,"overdue_bills":ob,
                   "open_complaints":oc,"visitors_today":vt,"pending_visitors":pv,"monthly_chart":chart})

    elif g.role in ("OWNER", "TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat: return ok({"role":g.role,"message":"No flat assigned yet","flat":None})
        fid = flat["id"]
        bills = r2l(qall("SELECT * FROM maintenance_bills WHERE flat_id=? ORDER BY bill_month DESC LIMIT 6",(fid,)))
        last_payment = r2d(qone("SELECT * FROM payments WHERE flat_id=? AND status='SUCCESS' ORDER BY created_at DESC LIMIT 1",(fid,)))
        pending_bills = qone("SELECT COUNT(*) c FROM maintenance_bills WHERE flat_id=? AND status IN ('PENDING','OVERDUE')",(fid,))["c"]
        my_visitors   = r2l(qall("SELECT * FROM visitor_logs WHERE flat_id=? AND DATE(created_at)=? ORDER BY created_at DESC",(fid,today_s)))
        pending_vis   = qone("SELECT COUNT(*) c FROM visitor_logs WHERE flat_id=? AND status='PENDING'",(fid,))["c"]
        my_complaints = r2l(qall("SELECT * FROM feedbacks WHERE flat_id=? ORDER BY created_at DESC LIMIT 5",(fid,)))
        return ok({"role":g.role,"flat":flat,"bills":bills,"last_payment":last_payment,
                   "pending_bills":pending_bills,"visitors_today":my_visitors,
                   "pending_visitors":pending_vis,"my_complaints":my_complaints})

    elif g.role == "GUARD":
        guard = r2d(qone("SELECT * FROM guards WHERE user_id=? AND is_active=1",(g.uid,)))
        gsid  = guard["society_id"] if guard else sid
        all_today = r2l(qall("SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.society_id=? AND DATE(v.created_at)=? ORDER BY v.created_at DESC",(gsid,today_s)))
        pending   = [v for v in all_today if v["status"]=="PENDING"]
        inside    = [v for v in all_today if v["status"] in ("APPROVED","INSIDE")]
        return ok({"role":"guard","guard":guard,"visitors_today":len(all_today),
                   "pending_count":len(pending),"inside_count":len(inside),
                   "pending_visitors":pending,"visitors":all_today})

    return ok({"role":g.role})

# ══════════════════════════════════════════════════════════════════════════════
# SOCIETIES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/societies", methods=["GET"])
@auth_required
def list_societies():
    page = int(request.args.get("page", 1))
    if g.role == "SUPER_ADMIN":
        rows = r2l(qall("SELECT * FROM societies WHERE is_active=1 ORDER BY name"))
    else:
        rows = r2l(qall(
            "SELECT s.* FROM societies s JOIN user_societies us ON us.society_id=s.id "
            "WHERE us.user_id=? AND us.is_active=1 AND s.is_active=1 ORDER BY s.name", (g.uid,)))
    items, meta = paginate(rows, page)
    return ok(items, meta=meta)

@app.route("/api/societies", methods=["POST"])
@admin_only
def create_society():
    d = request.json or {}
    if not d.get("name"): return err("Society name required")
    sid = qrun(
        "INSERT INTO societies(name,address,city,state,pincode,registration_number,total_flats,maintenance_due_day,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
        (d["name"],d.get("address"),d.get("city"),d.get("state"),d.get("pincode"),
         d.get("registration_number"),d.get("total_flats",0),d.get("maintenance_due_day",5),_now(),_now()))
    qrun("INSERT INTO user_societies(user_id,society_id,role,is_active) VALUES(?,?,?,1)", (g.uid,sid,"SOCIETY_ADMIN"))
    for wname in d.get("wings", []):
        qrun("INSERT INTO wings(society_id,name,total_floors) VALUES(?,?,10)", (sid, wname.strip()))
    return ok(r2d(qone("SELECT * FROM societies WHERE id=?", (sid,))))

@app.route("/api/societies/<int:sid>", methods=["GET"])
@auth_required
def get_society(sid):
    s = r2d(qone("SELECT * FROM societies WHERE id=?", (sid,)))
    if not s: return err("Not found", 404)
    s["wings"] = r2l(qall("SELECT * FROM wings WHERE society_id=?", (sid,)))
    s["bank_account"] = _mask_bank(r2d(qone("SELECT * FROM society_bank_accounts WHERE society_id=?", (sid,))))
    s["total_active_flats"] = qone("SELECT COUNT(*) c FROM flats WHERE society_id=? AND is_active=1", (sid,))["c"]
    s["plan"] = _get_society_plan(sid)
    return ok(s)

@app.route("/api/societies/<int:sid>", methods=["PUT"])
@admin_only
def update_society(sid):
    d = request.json or {}
    fields=[]; vals=[]
    for f in ["name","address","city","state","pincode","maintenance_due_day","total_flats"]:
        if f in d: fields.append(f"{f}=?"); vals.append(d[f])
    if fields: vals.extend([_now(),sid]); qrun(f"UPDATE societies SET {','.join(fields)},updated_at=? WHERE id=?",vals)
    return ok(r2d(qone("SELECT * FROM societies WHERE id=?", (sid,))))

@app.route("/api/societies/<int:sid>/wings", methods=["POST"])
@admin_only
def add_wing(sid):
    d = request.json or {}
    if not d.get("name"): return err("Wing name required")
    wid = qrun("INSERT INTO wings(society_id,name,total_floors) VALUES(?,?,?)", (sid,d["name"],d.get("total_floors",10)))
    return ok(r2d(qone("SELECT * FROM wings WHERE id=?", (wid,))))

# ══════════════════════════════════════════════════════════════════════════════
# FLATS  (admin: all flats | owner/tenant: their flat only)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/flats", methods=["GET"])
@auth_required
def list_flats():
    sid  = int(request.args.get("society_id", _get_user_society(g.uid)))
    page = int(request.args.get("page", 1))
    wing = request.args.get("wing_id"); stat = request.args.get("status")

    if g.role in ("OWNER","TENANT"):
        # Only their flat
        flat = _get_user_flat(g.uid)
        return ok([flat] if flat else [])

    sql = ("SELECT f.*,w.name wing_name FROM flats f "
           "LEFT JOIN wings w ON w.id=f.wing_id WHERE f.society_id=? AND f.is_active=1")
    params=[sid]
    if wing: sql+=" AND f.wing_id=?"; params.append(wing)
    if stat: sql+=" AND f.status=?";  params.append(stat)
    sql += " ORDER BY f.flat_number"
    rows = r2l(qall(sql, params))
    for r in rows:
        own = qone("SELECT u.name FROM flat_owners fo JOIN users u ON u.id=fo.user_id WHERE fo.flat_id=? AND fo.is_primary=1 AND fo.is_active=1",(r["id"],))
        ten = qone("SELECT u.name FROM tenants t JOIN users u ON u.id=t.user_id WHERE t.flat_id=? AND t.is_active=1",(r["id"],))
        r["owner_name"]  = own["name"] if own else None
        r["tenant_name"] = ten["name"] if ten else None
    items, meta = paginate(rows, page, 50)
    return ok(items, meta=meta)

@app.route("/api/flats/<int:fid>", methods=["GET"])
@auth_required
def get_flat(fid):
    f = r2d(qone("SELECT f.*,w.name wing_name FROM flats f LEFT JOIN wings w ON w.id=f.wing_id WHERE f.id=?",(fid,)))
    if not f: return err("Not found", 404)
    # Owners/tenants can only view their own flat
    if g.role in ("OWNER","TENANT"):
        my_flat = _get_user_flat(g.uid)
        if not my_flat or my_flat["id"] != fid: return err("Access denied", 403)
    f["owners"]  = r2l(qall("SELECT fo.*,u.name,u.mobile,u.email FROM flat_owners fo JOIN users u ON u.id=fo.user_id WHERE fo.flat_id=? AND fo.is_active=1",(fid,)))
    f["tenants"] = r2l(qall("SELECT t.*,u.name,u.mobile,u.email FROM tenants t JOIN users u ON u.id=t.user_id WHERE t.flat_id=? AND t.is_active=1",(fid,)))
    return ok(f)

@app.route("/api/flats", methods=["POST"])
@admin_only
def create_flat():
    d = request.json or {}
    if not d.get("flat_number"): return err("Flat number required")
    fid = qrun(
        "INSERT INTO flats(society_id,wing_id,flat_number,floor,flat_type,area_sqft,monthly_maintenance,status,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
        (d.get("society_id",1),d.get("wing_id"),d["flat_number"],d.get("floor"),
         d.get("flat_type"),d.get("area_sqft"),d.get("monthly_maintenance",3500),"VACANT",_now(),_now()))
    return ok(r2d(qone("SELECT * FROM flats WHERE id=?",(fid,))))

@app.route("/api/flats/<int:fid>", methods=["PUT"])
@admin_only
def update_flat(fid):
    d = request.json or {}
    fields=[]; vals=[]
    for f in ["flat_type","area_sqft","monthly_maintenance","status"]:
        if f in d: fields.append(f"{f}=?"); vals.append(d[f])
    if fields: vals.extend([_now(),fid]); qrun(f"UPDATE flats SET {','.join(fields)},updated_at=? WHERE id=?",vals)
    return ok(r2d(qone("SELECT * FROM flats WHERE id=?",(fid,))))

@app.route("/api/owners", methods=["POST"])
@admin_only
def add_owner():
    d = request.json or {}
    mobile = re.sub(r"\D","",str(d.get("mobile","")))
    if len(mobile)<10 or not d.get("flat_id"): return err("mobile and flat_id required")
    uid = _upsert_user(mobile, d.get("name"), d.get("email"), "OWNER")
    qrun("INSERT INTO flat_owners(flat_id,user_id,is_primary,is_active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
         (d["flat_id"],uid,1 if d.get("is_primary",True) else 0,_now(),_now()))
    qrun("UPDATE flats SET status='OWNER_OCCUPIED',updated_at=? WHERE id=?",(_now(),d["flat_id"]))
    # Link to society
    flat = qone("SELECT society_id FROM flats WHERE id=?",(d["flat_id"],))
    if flat:
        exists = qone("SELECT id FROM user_societies WHERE user_id=? AND society_id=?",(uid,flat["society_id"]))
        if not exists:
            qrun("INSERT INTO user_societies(user_id,society_id,role,is_active) VALUES(?,?,?,1)",(uid,flat["society_id"],"OWNER"))
    return ok({"user_id":uid,"message":"Owner added"})

@app.route("/api/tenants", methods=["POST"])
@auth_required
def add_tenant():
    d = request.json or {}
    if g.role not in ("SOCIETY_ADMIN","SUPER_ADMIN","MANAGER","OWNER"): return err("Access denied",403)
    mobile = re.sub(r"\D","",str(d.get("mobile","")))
    if len(mobile)<10 or not d.get("flat_id"): return err("mobile and flat_id required")
    # Owners can only add tenants to their own flat
    if g.role == "OWNER":
        my_flat = _get_user_flat(g.uid)
        if not my_flat or my_flat["id"] != int(d["flat_id"]): return err("Access denied",403)
    owner = qone("SELECT id FROM flat_owners WHERE flat_id=? AND is_primary=1 AND is_active=1",(d["flat_id"],))
    if not owner: return err("No primary owner found for this flat")
    uid = _upsert_user(mobile, d.get("name"), d.get("email"), "TENANT")
    qrun("INSERT INTO tenants(flat_id,user_id,owner_id,lease_start,lease_end,monthly_rent,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
         (d["flat_id"],uid,owner["id"],d.get("lease_start"),d.get("lease_end"),d.get("monthly_rent"),_now(),_now()))
    qrun("UPDATE flats SET status='TENANT_OCCUPIED',updated_at=? WHERE id=?",(_now(),d["flat_id"]))
    flat = qone("SELECT society_id FROM flats WHERE id=?",(d["flat_id"],))
    if flat:
        exists = qone("SELECT id FROM user_societies WHERE user_id=? AND society_id=?",(uid,flat["society_id"]))
        if not exists:
            qrun("INSERT INTO user_societies(user_id,society_id,role,is_active) VALUES(?,?,?,1)",(uid,flat["society_id"],"TENANT"))
    return ok({"user_id":uid,"message":"Tenant added"})

# ══════════════════════════════════════════════════════════════════════════════
# MAINTENANCE  (admin: all | owner/tenant: their flat)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/maintenance/bills", methods=["GET"])
@auth_required
@plan_required("maintenance")
def list_bills():
    sid  = int(request.args.get("society_id", _get_user_society(g.uid)))
    page = int(request.args.get("page", 1))
    try: month = date.fromisoformat(request.args.get("month",date.today().replace(day=1).isoformat())[:10]).replace(day=1).isoformat()
    except: month = date.today().replace(day=1).isoformat()

    if g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat: return ok({"bills":[],"summary":{}})
        rows = r2l(qall(
            "SELECT b.*,f.flat_number,w.name wing_name FROM maintenance_bills b "
            "JOIN flats f ON f.id=b.flat_id LEFT JOIN wings w ON w.id=f.wing_id "
            "WHERE b.flat_id=? ORDER BY b.bill_month DESC", (flat["id"],)))
        return ok({"bills":rows,"summary":{}})

    rows = r2l(qall(
        "SELECT b.*,f.flat_number,w.name wing_name FROM maintenance_bills b "
        "JOIN flats f ON f.id=b.flat_id LEFT JOIN wings w ON w.id=f.wing_id "
        "WHERE b.society_id=? AND b.bill_month=? ORDER BY f.flat_number", (sid,month)))
    sumq = r2d(qone(
        "SELECT COALESCE(SUM(CASE WHEN status='PAID' THEN total_amount ELSE 0 END),0) collected,"
        "COALESCE(SUM(CASE WHEN status='PENDING' THEN total_amount ELSE 0 END),0) pending,"
        "COALESCE(SUM(CASE WHEN status='OVERDUE' THEN total_amount ELSE 0 END),0) overdue,"
        "COUNT(*) total FROM maintenance_bills WHERE society_id=? AND bill_month=?", (sid,month)))
    items, meta = paginate(rows, page, 50)
    return ok({"bills":items,"summary":sumq}, meta=meta)

@app.route("/api/maintenance/generate", methods=["POST"])
@admin_only
@plan_required("maintenance")
def generate_bills():
    d = request.json or {}
    sid = int(d.get("society_id", 1))
    try: month = date.fromisoformat(d.get("month",date.today().replace(day=1).isoformat())[:10]).replace(day=1)
    except: month = date.today().replace(day=1)
    soc = qone("SELECT maintenance_due_day FROM societies WHERE id=?",(sid,))
    due_day = soc["maintenance_due_day"] if soc else 5
    try: due_date = date(month.year, month.month, due_day)
    except ValueError: due_date = date(month.year, month.month, 1)
    flats = r2l(qall("SELECT id,monthly_maintenance FROM flats WHERE society_id=? AND is_active=1 AND status!='VACANT'",(sid,)))
    created=skipped=0
    for flat in flats:
        if qone("SELECT id FROM maintenance_bills WHERE flat_id=? AND bill_month=?",(flat["id"],month.isoformat())):
            skipped+=1; continue
        amt=float(flat["monthly_maintenance"] or 0)
        qrun("INSERT INTO maintenance_bills(society_id,flat_id,bill_month,base_amount,penalty_amount,total_amount,due_date,status,created_at,updated_at) VALUES(?,?,?,?,0,?,?,?,?,?)",
             (sid,flat["id"],month.isoformat(),amt,amt,due_date.isoformat(),"PENDING",_now(),_now()))
        created+=1
    return ok({"created":created,"skipped":skipped,"month":month.isoformat()})

@app.route("/api/maintenance/bills/<int:bid>/pay", methods=["POST"])
@admin_only
def mark_bill_paid(bid):
    b = qone("SELECT * FROM maintenance_bills WHERE id=?",(bid,))
    if not b: return err("Not found",404)
    if b["status"]=="PAID": return err("Already paid")
    qrun("UPDATE maintenance_bills SET status='PAID',paid_at=?,updated_at=? WHERE id=?",(_now(),_now(),bid))
    return ok(r2d(qone("SELECT * FROM maintenance_bills WHERE id=?",(bid,))))

@app.route("/api/maintenance/apply-penalties", methods=["POST"])
@admin_only
@plan_required("maintenance")
def apply_penalties():
    d = request.json or {}
    sid = int(d.get("society_id",1))
    pct = float(CFG["features"].get("penalty_pct",2.0))/100
    today = date.today().isoformat()
    bills = r2l(qall("SELECT * FROM maintenance_bills WHERE society_id=? AND status='PENDING' AND due_date<?",(sid,today)))
    for b in bills:
        pen=round(float(b["base_amount"] or 0)*pct,2); tot=float(b["base_amount"] or 0)+pen
        qrun("UPDATE maintenance_bills SET status='OVERDUE',penalty_amount=?,total_amount=?,updated_at=? WHERE id=?",(pen,tot,_now(),b["id"]))
    return ok({"updated":len(bills)})

# ══════════════════════════════════════════════════════════════════════════════
# PAYMENTS  (all authenticated users can pay their own bills)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/payments/initiate", methods=["POST"])
@auth_required
@plan_required("payments")
def initiate_payment():
    d = request.json or {}
    fid = int(d.get("flat_id",0))
    flat = qone("SELECT * FROM flats WHERE id=?",(fid,))
    if not flat: return err("Flat not found",404)
    # Owners/tenants can only pay for their own flat
    if g.role in ("OWNER","TENANT"):
        my_flat = _get_user_flat(g.uid)
        if not my_flat or my_flat["id"]!=fid: return err("Access denied",403)
    plan = d.get("plan_type","MONTHLY")
    fee_pct  = Decimal(str(CFG["payment"]["processing_fee_pct"]))/100
    disc_pct = Decimal(str(CFG["payment"]["yearly_discount_pct"]))/100
    base = Decimal(str(flat["monthly_maintenance"] or 0))
    if plan=="YEARLY": base*=12
    disc = (base*disc_pct).quantize(Decimal("0.01")) if plan=="YEARLY" else Decimal("0")
    after = base-disc; fee = (after*fee_pct).quantize(Decimal("0.01")); total = after+fee
    return ok({"flat_id":fid,"plan_type":plan,"base_amount":float(base),
               "discount_amount":float(disc),"processing_fee":float(fee),"total_amount":float(total),
               "gateway":CFG["payment"]["gateway"],"period_start":d.get("period_start"),"period_end":d.get("period_end")})

@app.route("/api/payments/confirm", methods=["POST"])
@auth_required
@plan_required("payments")
def confirm_payment():
    d = request.json or {}
    fid  = int(d.get("flat_id",0))
    flat = qone("SELECT * FROM flats WHERE id=?",(fid,))
    if not flat: return err("Flat not found",404)
    if g.role in ("OWNER","TENANT"):
        my_flat = _get_user_flat(g.uid)
        if not my_flat or my_flat["id"]!=fid: return err("Access denied",403)
    plan = d.get("plan_type","MONTHLY")
    fee_pct  = Decimal(str(CFG["payment"]["processing_fee_pct"]))/100
    disc_pct = Decimal(str(CFG["payment"]["yearly_discount_pct"]))/100
    base = Decimal(str(flat["monthly_maintenance"] or 0))
    if plan=="YEARLY": base*=12
    disc = (base*disc_pct).quantize(Decimal("0.01")) if plan=="YEARLY" else Decimal("0")
    after = base-disc; fee = (after*fee_pct).quantize(Decimal("0.01")); total = after+fee
    rno = gen_receipt(flat["society_id"])
    pid = qrun(
        "INSERT INTO payments(receipt_number,society_id,flat_id,paid_by,plan_type,period_start,period_end,base_amount,discount_amount,processing_fee,total_amount,payment_method,upi_app,card_last4,status,completed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rno,flat["society_id"],fid,g.uid,plan,d.get("period_start"),d.get("period_end"),
         float(base),float(disc),float(fee),float(total),
         d.get("payment_method"),d.get("upi_app"),d.get("card_last4"),"SUCCESS",_now(),_now(),_now()))
    try:
        ps=d.get("period_start","")[:7]; pe=d.get("period_end","")[:7]
        if ps:
            get_db().execute("UPDATE maintenance_bills SET status='PAID',paid_at=? WHERE flat_id=? AND status!='PAID' AND bill_month>=? AND bill_month<=?",
                (_now(),fid,ps+"-01",(pe+"-01") if pe else ps+"-01"))
            get_db().commit()
    except Exception as e: log.warning(f"Bill auto-pay: {e}")
    return ok({"payment":r2d(qone("SELECT * FROM payments WHERE id=?",(pid,))),"receipt_number":rno})

@app.route("/api/payments/history", methods=["GET"])
@auth_required
@plan_required("payment_history")
def payment_history():
    page = int(request.args.get("page",1))
    if g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat: return ok([])
        rows = r2l(qall("SELECT p.*,f.flat_number FROM payments p LEFT JOIN flats f ON f.id=p.flat_id WHERE p.flat_id=? ORDER BY p.created_at DESC",(flat["id"],)))
    else:
        sid = int(request.args.get("society_id",1))
        fid = request.args.get("flat_id",type=int)
        sql = "SELECT p.*,f.flat_number FROM payments p LEFT JOIN flats f ON f.id=p.flat_id WHERE p.society_id=?"
        params=[sid]
        if fid: sql+=" AND p.flat_id=?"; params.append(fid)
        sql+=" ORDER BY p.created_at DESC"
        rows = r2l(qall(sql,params))
    items, meta = paginate(rows, page, 20)
    return ok(items, meta=meta)

# ══════════════════════════════════════════════════════════════════════════════
# BANK ACCOUNT  (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/bank/<int:sid>", methods=["GET"])
@auth_required
@plan_required("bank_account")
def get_bank(sid):
    return ok(_mask_bank(r2d(qone("SELECT * FROM society_bank_accounts WHERE society_id=?",(sid,)))))

@app.route("/api/bank", methods=["POST"])
@admin_only
@plan_required("bank_account")
def link_bank():
    d = request.json or {}
    sid = int(d.get("society_id",1))
    ah=(d.get("account_holder") or "").strip(); an=(d.get("account_number") or "").strip()
    anc=(d.get("confirm_account_number") or "").strip(); ifc=(d.get("ifsc_code") or "").strip().upper()
    if not ah or not an or not ifc: return err("account_holder, account_number and ifsc_code are required")
    if an!=anc: return err("Account numbers do not match")
    if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$",ifc): return err("Invalid IFSC code format (e.g. HDFC0002341)")
    if len(an)<8: return err("Account number must be at least 8 digits")
    if qone("SELECT id FROM society_bank_accounts WHERE society_id=?",(sid,)):
        qrun("UPDATE society_bank_accounts SET account_holder=?,account_number=?,ifsc_code=?,bank_name=?,account_type=?,is_verified=1,verified_at=?,linked_by=?,updated_at=? WHERE society_id=?",
             (ah,an,ifc,d.get("bank_name"),d.get("account_type","SAVINGS"),_now(),g.uid,_now(),sid))
    else:
        qrun("INSERT INTO society_bank_accounts(society_id,account_holder,account_number,ifsc_code,bank_name,account_type,is_verified,verified_at,linked_by,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
             (sid,ah,an,ifc,d.get("bank_name"),d.get("account_type","SAVINGS"),_now(),g.uid,_now(),_now()))
    return ok(_mask_bank(r2d(qone("SELECT * FROM society_bank_accounts WHERE society_id=?",(sid,)))))

# ══════════════════════════════════════════════════════════════════════════════
# VISITORS  (all roles, different views)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/visitors", methods=["GET"])
@auth_required
def list_visitors():
    page  = int(request.args.get("page",1))
    stat  = request.args.get("status")
    date_ = request.args.get("date")

    if g.role == "GUARD":
        guard = r2d(qone("SELECT * FROM guards WHERE user_id=? AND is_active=1",(g.uid,)))
        gsid  = guard["society_id"] if guard else 1
        today = date.today().isoformat()
        sql   = "SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.society_id=?"
        params= [gsid]
        if stat:  sql+=" AND v.status=?";           params.append(stat)
        if date_: sql+=" AND DATE(v.created_at)=?"; params.append(date_)
        else:     sql+=" AND DATE(v.created_at)=?"; params.append(today)
        sql+=" ORDER BY v.created_at DESC"

    elif g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat: return ok([])
        sql = "SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.flat_id=?"
        params=[flat["id"]]
        if stat: sql+=" AND v.status=?"; params.append(stat)
        sql+=" ORDER BY v.created_at DESC"

    else:  # Admin
        sid = int(request.args.get("society_id",1))
        sql = "SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.society_id=?"
        params=[sid]
        if stat:  sql+=" AND v.status=?";           params.append(stat)
        if date_: sql+=" AND DATE(v.created_at)=?"; params.append(date_)
        sql+=" ORDER BY v.created_at DESC"

    rows  = r2l(qall(sql, params))
    items, meta = paginate(rows, page, 30)
    return ok(items, meta=meta)

@app.route("/api/visitors", methods=["POST"])
@auth_required
def log_visitor():
    d = request.json or {}
    if not d.get("visitor_name"): return err("visitor_name required")
    # Determine flat_id and society_id
    if g.role == "GUARD":
        if not d.get("flat_id"): return err("flat_id required")
        guard = r2d(qone("SELECT * FROM guards WHERE user_id=? AND is_active=1",(g.uid,)))
        gsid  = guard["society_id"] if guard else int(d.get("society_id",1))
        flat_id = int(d["flat_id"]); sid = gsid
    elif g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat: return err("No flat assigned")
        flat_id = flat["id"]; sid = flat["society_id"]
    else:
        if not d.get("flat_id"): return err("flat_id required")
        flat_id = int(d["flat_id"]); sid = int(d.get("society_id",1))

    vid = qrun(
        "INSERT INTO visitor_logs(society_id,flat_id,visitor_name,visitor_mobile,vehicle_number,purpose,visit_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid,flat_id,d["visitor_name"],d.get("visitor_mobile"),d.get("vehicle_number"),
         d.get("purpose"),d.get("visit_type","GUEST"),"PENDING",_now(),_now()))
    return ok(r2d(qone("SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",(vid,))))

@app.route("/api/visitors/<int:vid>/approve", methods=["PUT"])
@auth_required
def approve_visitor(vid):
    v = qone("SELECT * FROM visitor_logs WHERE id=?",(vid,))
    if not v: return err("Not found",404)
    # Owners/tenants can only approve visitors for their flat
    if g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat or flat["id"] != v["flat_id"]: return err("Access denied",403)
    qrun("UPDATE visitor_logs SET status='APPROVED',approved_by=?,approved_at=?,entry_time=?,updated_at=? WHERE id=?",(g.uid,_now(),_now(),_now(),vid))
    return ok(r2d(qone("SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",(vid,))))

@app.route("/api/visitors/<int:vid>/reject", methods=["PUT"])
@auth_required
def reject_visitor(vid):
    v = qone("SELECT * FROM visitor_logs WHERE id=?",(vid,))
    if not v: return err("Not found",404)
    if g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        if not flat or flat["id"] != v["flat_id"]: return err("Access denied",403)
    qrun("UPDATE visitor_logs SET status='REJECTED',approved_by=?,approved_at=?,updated_at=? WHERE id=?",(g.uid,_now(),_now(),vid))
    return ok(r2d(qone("SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",(vid,))))

@app.route("/api/visitors/<int:vid>/exit", methods=["PUT"])
@auth_required
def exit_visitor(vid):
    qrun("UPDATE visitor_logs SET status='EXITED',exit_time=?,updated_at=? WHERE id=?",(_now(),_now(),vid))
    return ok(r2d(qone("SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",(vid,))))

# ══════════════════════════════════════════════════════════════════════════════
# GUARDS  (admin management, guard self-view)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/guards", methods=["GET"])
@auth_required
def list_guards():
    sid = int(request.args.get("society_id",1))
    return ok(r2l(qall("SELECT g.*,u.name,u.mobile FROM guards g JOIN users u ON u.id=g.user_id WHERE g.society_id=? AND g.is_active=1 ORDER BY g.shift",(sid,))))

@app.route("/api/guards", methods=["POST"])
@admin_only
def add_guard():
    d = request.json or {}
    mobile = re.sub(r"\D","",str(d.get("mobile","")))
    if len(mobile)<10: return err("Mobile required")
    uid = _upsert_user(mobile, d.get("name"), None, "GUARD")
    gid = qrun("INSERT INTO guards(user_id,society_id,employee_id,shift,gate_assigned,is_active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
               (uid,int(d.get("society_id",1)),d.get("employee_id"),d.get("shift","MORNING"),d.get("gate_assigned"),_now(),_now()))
    flat = qone("SELECT id FROM societies WHERE id=?", (d.get("society_id",1),))
    exists = qone("SELECT id FROM user_societies WHERE user_id=? AND society_id=?",(uid,d.get("society_id",1)))
    if not exists:
        qrun("INSERT INTO user_societies(user_id,society_id,role,is_active) VALUES(?,?,?,1)",(uid,d.get("society_id",1),"GUARD"))
    return ok(r2d(qone("SELECT g.*,u.name,u.mobile FROM guards g JOIN users u ON u.id=g.user_id WHERE g.id=?",(gid,))))

@app.route("/api/guards/<int:gid>", methods=["PUT"])
@admin_only
def update_guard(gid):
    d = request.json or {}
    fields=[]; vals=[]
    for f in ["shift","gate_assigned","is_active","employee_id"]:
        if f in d: fields.append(f"{f}=?"); vals.append(d[f])
    if fields: vals.extend([_now(),gid]); qrun(f"UPDATE guards SET {','.join(fields)},updated_at=? WHERE id=?",vals)
    return ok(r2d(qone("SELECT g.*,u.name,u.mobile FROM guards g JOIN users u ON u.id=g.user_id WHERE g.id=?",(gid,))))

# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK  (all roles can submit; admin manages)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/feedback", methods=["GET"])
@auth_required
def list_feedback():
    page = int(request.args.get("page",1))
    if g.role in ("OWNER","TENANT"):
        flat = _get_user_flat(g.uid)
        sql = "SELECT fb.*,u.name submitted_by_name,f.flat_number FROM feedbacks fb LEFT JOIN users u ON u.id=fb.submitted_by LEFT JOIN flats f ON f.id=fb.flat_id WHERE fb.submitted_by=?"
        params=[g.uid]
    else:
        sid = int(request.args.get("society_id",1))
        stat = request.args.get("status")
        sql = "SELECT fb.*,u.name submitted_by_name,f.flat_number FROM feedbacks fb LEFT JOIN users u ON u.id=fb.submitted_by LEFT JOIN flats f ON f.id=fb.flat_id WHERE fb.society_id=?"
        params=[sid]
        if stat: sql+=" AND fb.status=?"; params.append(stat)
    sql+=" ORDER BY fb.created_at DESC"
    items, meta = paginate(r2l(qall(sql,params)), page, 20)
    return ok(items, meta=meta)

@app.route("/api/feedback", methods=["POST"])
@auth_required
def submit_feedback():
    d = request.json or {}
    if not d.get("title"): return err("Title required")
    sid = int(d.get("society_id", _get_user_society(g.uid)))
    flat = _get_user_flat(g.uid)
    fid = flat["id"] if flat else d.get("flat_id")
    fid2 = qrun(
        "INSERT INTO feedbacks(society_id,flat_id,submitted_by,category,title,description,priority,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid,fid,g.uid,d.get("category"),d["title"],d.get("description"),d.get("priority","MEDIUM"),"OPEN",_now(),_now()))
    return ok(r2d(qone("SELECT * FROM feedbacks WHERE id=?",(fid2,))))

@app.route("/api/feedback/<int:fid>", methods=["PUT"])
@auth_required
def update_feedback(fid):
    fb = qone("SELECT * FROM feedbacks WHERE id=?",(fid,))
    if not fb: return err("Not found",404)
    # Only admin can update status; submitter can add rating
    d = request.json or {}
    fields=[]; vals=[]
    if g.role in ("SOCIETY_ADMIN","SUPER_ADMIN","MANAGER"):
        for f in ["status","priority","assigned_to"]:
            if f in d: fields.append(f"{f}=?"); vals.append(d[f])
        if d.get("status")=="RESOLVED": fields.append("resolved_at=?"); vals.append(_now())
    if "rating" in d and fb["submitted_by"]==g.uid:
        fields.append("rating=?"); vals.append(d["rating"])
    if fields: vals.extend([_now(),fid]); qrun(f"UPDATE feedbacks SET {','.join(fields)},updated_at=? WHERE id=?",vals)
    return ok(r2d(qone("SELECT * FROM feedbacks WHERE id=?",(fid,))))

# ══════════════════════════════════════════════════════════════════════════════
# ANNOUNCEMENTS  (all can read; admin publishes)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/announcements", methods=["GET"])
@auth_required
def list_announcements():
    sid  = int(request.args.get("society_id", _get_user_society(g.uid)))
    page = int(request.args.get("page",1))
    items, meta = paginate(r2l(qall(
        "SELECT a.*,u.name published_by_name FROM announcements a "
        "LEFT JOIN users u ON u.id=a.published_by WHERE a.society_id=? "
        "ORDER BY a.is_pinned DESC,a.created_at DESC", (sid,))), page, 20)
    return ok(items, meta=meta)

@app.route("/api/announcements", methods=["POST"])
@admin_only
def create_announcement():
    d = request.json or {}
    if not d.get("title"): return err("Title required")
    aid = qrun(
        "INSERT INTO announcements(society_id,title,content,category,is_pinned,published_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (int(d.get("society_id",1)),d["title"],d.get("content"),d.get("category","GENERAL"),
         1 if d.get("is_pinned") else 0,g.uid,_now(),_now()))
    return ok(r2d(qone("SELECT a.*,u.name published_by_name FROM announcements a LEFT JOIN users u ON u.id=a.published_by WHERE a.id=?",(aid,))))

@app.route("/api/announcements/<int:aid>", methods=["DELETE"])
@admin_only
def delete_announcement(aid):
    qrun("DELETE FROM announcements WHERE id=?",(aid,))
    return ok({"deleted":True})

# ══════════════════════════════════════════════════════════════════════════════
# SERVICE PROVIDERS  (PAID feature — electrician, plumber, carpenter, etc.)
# ══════════════════════════════════════════════════════════════════════════════

SERVICE_CATEGORIES = ["ELECTRICIAN","PLUMBER","CARPENTER","PAINTER","MAID","CLEANING","AC_TECHNICIAN","PEST_CONTROL","OTHER"]
NEGATIVE_AUTO_FLAG_THRESHOLD = 5   # flag provider for review after N negatives

def _gen_provider_code(category):
    """Generate unique provider code e.g. SP-PLMB-2026-001"""
    cat_codes = {"ELECTRICIAN":"ELEC","PLUMBER":"PLMB","CARPENTER":"CRPT",
                 "PAINTER":"PNTR","MAID":"MAID","CLEANING":"CLNG",
                 "AC_TECHNICIAN":"ACTN","PEST_CONTROL":"PEST","OTHER":"OTHR"}
    code = cat_codes.get(category, "OTHR")
    year = datetime.utcnow().year
    last = qone(f"SELECT COUNT(*) c FROM service_providers WHERE provider_code LIKE 'SP-{code}-{year}-%'")
    seq  = (last["c"] if last else 0) + 1
    return f"SP-{code}-{year}-{seq:04d}"

def _update_provider_rating(pid):
    """Recompute and save rating stats for a provider."""
    stats = qone(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN rating_type='POSITIVE' THEN 1 ELSE 0 END) pos, "
        "SUM(CASE WHEN rating_type='NEGATIVE' THEN 1 ELSE 0 END) neg, "
        "AVG(CASE WHEN rating_type='POSITIVE' THEN rating_score ELSE NULL END) avg_pos "
        "FROM service_ratings WHERE provider_id=?", (pid,))
    total = stats["total"] or 0
    pos   = stats["pos"] or 0
    neg   = stats["neg"] or 0
    avg   = round(float(stats["avg_pos"] or 0), 1)
    auto_flag = 1 if neg >= NEGATIVE_AUTO_FLAG_THRESHOLD else 0
    qrun("UPDATE service_providers SET rating_count=?,positive_count=?,negative_count=?,rating_avg=?,auto_flag=?,updated_at=? WHERE id=?",
         (total, pos, neg, avg, auto_flag, _now(), pid))

# ── List providers ─────────────────────────────────────────────────────────────
@app.route("/api/services/providers", methods=["GET"])
@auth_required
@plan_required("service_directory")
def list_providers():
    sid      = int(request.args.get("society_id", _get_user_society(g.uid)))
    category = request.args.get("category")
    status   = request.args.get("status", "APPROVED")  # default approved only
    page     = int(request.args.get("page", 1))

    if g.role == "SUPER_ADMIN":
        # SA sees all providers platform-wide
        sql = "SELECT sp.* FROM service_providers sp WHERE 1=1"
        params = []
        if category: sql += " AND sp.category=?"; params.append(category)
        if status != "ALL": sql += " AND sp.verification_status=?"; params.append(status)
    else:
        # Society users see providers linked to their society (approved only)
        sql = ("SELECT sp.* FROM service_providers sp "
               "JOIN provider_society_links psl ON psl.provider_id=sp.id "
               "WHERE psl.society_id=? AND psl.is_active=1 AND sp.verification_status='APPROVED' AND sp.is_blocked=0")
        params = [sid]
        if category: sql += " AND sp.category=?"; params.append(category)

    sql += " ORDER BY sp.rating_avg DESC, sp.created_at DESC"
    items, meta = paginate(r2l(qall(sql, params)), page, 20)
    return ok(items, meta=meta)

# ── Get single provider ────────────────────────────────────────────────────────
@app.route("/api/services/providers/<int:pid>", methods=["GET"])
@auth_required
@plan_required("service_directory")
def get_provider(pid):
    p = r2d(qone("SELECT * FROM service_providers WHERE id=?", (pid,)))
    if not p: return err("Provider not found", 404)
    p["ratings"] = r2l(qall(
        "SELECT sr.*,u.name rated_by_name,f.flat_number FROM service_ratings sr "
        "LEFT JOIN users u ON u.id=sr.rated_by "
        "LEFT JOIN flats f ON f.id=sr.flat_id "
        "WHERE sr.provider_id=? ORDER BY sr.created_at DESC LIMIT 20", (pid,)))
    p["societies"] = r2l(qall(
        "SELECT s.id,s.name FROM provider_society_links psl "
        "JOIN societies s ON s.id=psl.society_id "
        "WHERE psl.provider_id=? AND psl.is_active=1", (pid,)))
    return ok(p)

# ── Add new provider (anyone can submit — goes PENDING for SA approval) ────────
@app.route("/api/services/providers", methods=["POST"])
@auth_required
@plan_required("service_directory")
def add_provider():
    d = request.json or {}
    if not d.get("name"):     return err("Provider name required")
    if not d.get("mobile"):   return err("Provider mobile required")
    if not d.get("category"): return err("Category required")
    cat = d["category"].upper()
    if cat not in SERVICE_CATEGORIES: return err(f"Category must be one of {SERVICE_CATEGORIES}")

    # Check duplicate mobile
    existing = qone("SELECT id,name,verification_status FROM service_providers WHERE mobile=?", (d["mobile"],))
    if existing: return err(f"Provider with mobile {d['mobile']} already exists (ID: {existing['id']}, Status: {existing['verification_status']})")

    sid = int(d.get("society_id", _get_user_society(g.uid)))

    # SUPER_ADMIN gets auto-approved with code
    if g.role == "SUPER_ADMIN":
        code = _gen_provider_code(cat)
        pid = qrun(
            "INSERT INTO service_providers(provider_code,name,mobile,email,category,specializations,experience_years,hourly_rate,bio,id_proof_type,id_proof_number,verification_status,is_active,is_blocked,requested_by,approved_by,approved_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'APPROVED',1,0,?,?,?,?,?)",
            (code,d["name"],d["mobile"],d.get("email"),cat,d.get("specializations"),
             int(d.get("experience_years",0)),d.get("hourly_rate"),d.get("bio"),
             d.get("id_proof_type"),d.get("id_proof_number"),
             g.uid,g.uid,_now(),_now(),_now()))
    else:
        pid = qrun(
            "INSERT INTO service_providers(name,mobile,email,category,specializations,experience_years,hourly_rate,bio,id_proof_type,id_proof_number,verification_status,is_active,is_blocked,requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING',1,0,?,?,?)",
            (d["name"],d["mobile"],d.get("email"),cat,d.get("specializations"),
             int(d.get("experience_years",0)),d.get("hourly_rate"),d.get("bio"),
             d.get("id_proof_type"),d.get("id_proof_number"),g.uid,_now(),_now()))

    # Link to society
    qrun("INSERT OR IGNORE INTO provider_society_links(provider_id,society_id,is_active,added_by,added_at) VALUES(?,?,1,?,?)",
         (pid, sid, g.uid, _now()))

    p = r2d(qone("SELECT * FROM service_providers WHERE id=?", (pid,)))
    return ok(p)

# ── Link existing provider to a society ───────────────────────────────────────
@app.route("/api/services/providers/<int:pid>/link", methods=["POST"])
@auth_required
def link_provider_society(pid):
    d = request.json or {}
    sid = int(d.get("society_id", _get_user_society(g.uid)))
    if g.role not in ("SUPER_ADMIN","SOCIETY_ADMIN"): return err("Admin access required", 403)
    qrun("INSERT OR IGNORE INTO provider_society_links(provider_id,society_id,is_active,added_by,added_at) VALUES(?,?,1,?,?)",
         (pid, sid, g.uid, _now()))
    return ok({"message": "Provider linked to society"})

# ── SUPER_ADMIN: approve provider ──────────────────────────────────────────────
@app.route("/api/services/providers/<int:pid>/approve", methods=["PUT"])
@super_only
def approve_provider(pid):
    p = qone("SELECT * FROM service_providers WHERE id=?", (pid,))
    if not p: return err("Not found", 404)
    code = p["provider_code"] or _gen_provider_code(p["category"])
    qrun("UPDATE service_providers SET verification_status='APPROVED',provider_code=?,approved_by=?,approved_at=?,updated_at=? WHERE id=?",
         (code, g.uid, _now(), _now(), pid))
    return ok(r2d(qone("SELECT * FROM service_providers WHERE id=?", (pid,))))

# ── SUPER_ADMIN: reject provider ───────────────────────────────────────────────
@app.route("/api/services/providers/<int:pid>/reject", methods=["PUT"])
@super_only
def reject_provider(pid):
    d = request.json or {}
    qrun("UPDATE service_providers SET verification_status='REJECTED',rejection_note=?,approved_by=?,approved_at=?,updated_at=? WHERE id=?",
         (d.get("note","Rejected"), g.uid, _now(), _now(), pid))
    return ok(r2d(qone("SELECT * FROM service_providers WHERE id=?", (pid,))))

# ── SUPER_ADMIN: block / unblock provider ─────────────────────────────────────
@app.route("/api/services/providers/<int:pid>/block", methods=["PUT"])
@super_only
def block_provider(pid):
    d = request.json or {}
    qrun("UPDATE service_providers SET is_blocked=1,block_reason=?,updated_at=? WHERE id=?",
         (d.get("reason","Blocked by Super Admin"), _now(), pid))
    return ok({"message": "Provider blocked", "id": pid})

@app.route("/api/services/providers/<int:pid>/unblock", methods=["PUT"])
@super_only
def unblock_provider(pid):
    qrun("UPDATE service_providers SET is_blocked=0,block_reason=NULL,auto_flag=0,updated_at=? WHERE id=?",
         (_now(), pid))
    return ok({"message": "Provider unblocked", "id": pid})

# ── Service requests (appointments) ───────────────────────────────────────────
@app.route("/api/services/requests", methods=["GET"])
@auth_required
@plan_required("service_requests")
def list_service_requests():
    page = int(request.args.get("page", 1))
    if g.role == "SUPER_ADMIN":
        sql    = "SELECT sr.*,sp.name provider_name,sp.category,sp.provider_code,f.flat_number FROM service_requests sr LEFT JOIN service_providers sp ON sp.id=sr.provider_id LEFT JOIN flats f ON f.id=sr.flat_id WHERE 1=1"
        params = []
    elif g.role in ("SOCIETY_ADMIN",):
        sid    = int(request.args.get("society_id", _get_user_society(g.uid)))
        sql    = "SELECT sr.*,sp.name provider_name,sp.category,sp.provider_code,f.flat_number FROM service_requests sr LEFT JOIN service_providers sp ON sp.id=sr.provider_id LEFT JOIN flats f ON f.id=sr.flat_id WHERE sr.society_id=?"
        params = [sid]
    else:
        flat   = _get_user_flat(g.uid)
        if not flat: return ok([])
        sql    = "SELECT sr.*,sp.name provider_name,sp.category,sp.provider_code,f.flat_number FROM service_requests sr LEFT JOIN service_providers sp ON sp.id=sr.provider_id LEFT JOIN flats f ON f.id=sr.flat_id WHERE sr.flat_id=?"
        params = [flat["id"]]
    stat = request.args.get("status")
    if stat: sql += " AND sr.status=?"; params.append(stat)
    sql += " ORDER BY sr.created_at DESC"
    items, meta = paginate(r2l(qall(sql, params)), page, 20)
    return ok(items, meta=meta)

@app.route("/api/services/requests", methods=["POST"])
@auth_required
@plan_required("service_requests")
def create_service_request():
    d = request.json or {}
    if not d.get("provider_id"):         return err("provider_id required")
    if not d.get("service_category"):    return err("service_category required")
    flat = _get_user_flat(g.uid)
    if not flat: return err("No flat assigned to your account")
    p = qone("SELECT * FROM service_providers WHERE id=? AND verification_status='APPROVED' AND is_blocked=0 AND is_active=1", (d["provider_id"],))
    if not p: return err("Provider not found or not available")
    rid = qrun(
        "INSERT INTO service_requests(society_id,flat_id,requested_by,provider_id,service_category,description,preferred_date,preferred_time,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (flat["society_id"],flat["id"],g.uid,d["provider_id"],d["service_category"],
         d.get("description"),d.get("preferred_date"),d.get("preferred_time"),"PENDING",_now(),_now()))
    return ok(r2d(qone("SELECT sr.*,sp.name provider_name,sp.provider_code FROM service_requests sr LEFT JOIN service_providers sp ON sp.id=sr.provider_id WHERE sr.id=?",(rid,))))

@app.route("/api/services/requests/<int:rid>/confirm", methods=["PUT"])
@auth_required
def confirm_service_request(rid):
    if g.role not in ("SUPER_ADMIN","SOCIETY_ADMIN"): return err("Admin access required", 403)
    d = request.json or {}
    qrun("UPDATE service_requests SET status='CONFIRMED',confirmed_at=?,scheduled_date=?,scheduled_time=?,updated_at=? WHERE id=?",
         (_now(),d.get("scheduled_date"),d.get("scheduled_time"),_now(),rid))
    return ok(r2d(qone("SELECT * FROM service_requests WHERE id=?",(rid,))))

@app.route("/api/services/requests/<int:rid>/complete", methods=["PUT"])
@auth_required
def complete_service_request(rid):
    if g.role not in ("SUPER_ADMIN","SOCIETY_ADMIN"): return err("Admin access required", 403)
    d = request.json or {}
    qrun("UPDATE service_requests SET status='COMPLETED',completed_at=?,completion_note=?,updated_at=? WHERE id=?",
         (_now(),d.get("note"),_now(),rid))
    return ok(r2d(qone("SELECT * FROM service_requests WHERE id=?",(rid,))))

@app.route("/api/services/requests/<int:rid>/cancel", methods=["PUT"])
@auth_required
def cancel_service_request(rid):
    req = qone("SELECT * FROM service_requests WHERE id=?",(rid,))
    if not req: return err("Not found", 404)
    if g.role in ("OWNER","TENANT") and req["requested_by"]!=g.uid: return err("Access denied",403)
    d = request.json or {}
    qrun("UPDATE service_requests SET status='CANCELLED',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?",
         (_now(),d.get("reason"),_now(),rid))
    return ok({"message":"Request cancelled"})

# ── Rate a provider after service ─────────────────────────────────────────────
@app.route("/api/services/requests/<int:rid>/rate", methods=["POST"])
@auth_required
def rate_provider(rid):
    req = qone("SELECT * FROM service_requests WHERE id=?",(rid,))
    if not req: return err("Service request not found",404)
    if req["status"] not in ("COMPLETED","CONFIRMED"): return err("Can only rate completed or confirmed services")
    if g.role in ("OWNER","TENANT") and req["requested_by"]!=g.uid: return err("Can only rate your own requests",403)
    # Check already rated
    if qone("SELECT id FROM service_ratings WHERE request_id=?",(rid,)):
        return err("You have already rated this service")
    d = request.json or {}
    rtype = (d.get("rating_type") or "").upper()
    if rtype not in ("POSITIVE","NEGATIVE"): return err("rating_type must be POSITIVE or NEGATIVE")
    score = max(1, min(5, int(d.get("rating_score", 5 if rtype=="POSITIVE" else 1))))
    flat  = _get_user_flat(g.uid)
    qrun("INSERT INTO service_ratings(request_id,provider_id,flat_id,society_id,rated_by,rating_type,rating_score,comment,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
         (rid,req["provider_id"],req["flat_id"],req["society_id"],g.uid,rtype,score,d.get("comment"),_now()))
    # Update provider rating stats
    _update_provider_rating(req["provider_id"])
    # Check auto-flag
    prov = qone("SELECT negative_count,auto_flag,name FROM service_providers WHERE id=?",(req["provider_id"],))
    msg  = f"Rating submitted! "
    if prov and prov["auto_flag"]:
        msg += f"Provider '{prov['name']}' has been flagged for review due to {prov['negative_count']} negative ratings."
    return ok({"message": msg, "auto_flagged": bool(prov and prov["auto_flag"])})

# ── Platform-wide service stats for SUPER_ADMIN ────────────────────────────────
@app.route("/api/services/stats", methods=["GET"])
@super_only
def service_stats():
    total       = qone("SELECT COUNT(*) c FROM service_providers")["c"]
    approved    = qone("SELECT COUNT(*) c FROM service_providers WHERE verification_status='APPROVED'")["c"]
    pending     = qone("SELECT COUNT(*) c FROM service_providers WHERE verification_status='PENDING'")["c"]
    blocked     = qone("SELECT COUNT(*) c FROM service_providers WHERE is_blocked=1")["c"]
    flagged     = qone("SELECT COUNT(*) c FROM service_providers WHERE auto_flag=1 AND is_blocked=0")["c"]
    req_total   = qone("SELECT COUNT(*) c FROM service_requests")["c"]
    req_pending = qone("SELECT COUNT(*) c FROM service_requests WHERE status='PENDING'")["c"]
    by_cat      = r2l(qall("SELECT category, COUNT(*) c FROM service_providers WHERE verification_status='APPROVED' GROUP BY category"))
    flagged_prov= r2l(qall("SELECT * FROM service_providers WHERE auto_flag=1 AND is_blocked=0 ORDER BY negative_count DESC LIMIT 10"))
    pending_prov= r2l(qall("SELECT * FROM service_providers WHERE verification_status='PENDING' ORDER BY created_at ASC LIMIT 20"))
    return ok({"total":total,"approved":approved,"pending":pending,"blocked":blocked,
               "flagged":flagged,"req_total":req_total,"req_pending":req_pending,
               "by_category":by_cat,"flagged_providers":flagged_prov,"pending_providers":pending_prov})


# ══════════════════════════════════════════════════════════════════════════════
# SOCIETY EXPENSES  (monthly expense tracking by admin)
# ══════════════════════════════════════════════════════════════════════════════

EXPENSE_CATEGORIES = [
    "WATER","ELECTRICITY","LIFT_AMC","SECURITY","CLEANING","GARDENING",
    "PEST_CONTROL","REPAIRS","INSURANCE","INTERNET","SALARY","LEGAL",
    "EVENTS","MISCELLANEOUS"
]

@app.route("/api/expenses", methods=["GET"])
@auth_required
def list_expenses():
    sid  = int(request.args.get("society_id", _get_user_society(g.uid)))
    page = int(request.args.get("page", 1))
    today = datetime.utcnow()
    month = request.args.get("month", f"{today.year}-{today.month:02d}-01")
    cat   = request.args.get("category")
    sql   = "SELECT e.*,u.name added_by_name FROM society_expenses e LEFT JOIN users u ON u.id=e.added_by WHERE e.society_id=? AND e.expense_month=?"
    params = [sid, month[:7]+"-01"]
    if cat: sql += " AND e.category=?"; params.append(cat)
    sql += " ORDER BY e.created_at DESC"
    rows = r2l(qall(sql, params))
    total_amt = sum(r["amount"] for r in rows)
    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amount"]
    items, meta = paginate(rows, page, 30)
    return ok({"expenses": items, "summary": {"total": total_amt, "by_category": by_cat, "count": len(rows)}}, meta=meta)

@app.route("/api/expenses/summary", methods=["GET"])
@auth_required
def expense_summary():
    """6-month trend summary for charts"""
    sid = int(request.args.get("society_id", _get_user_society(g.uid)))
    today = datetime.utcnow()
    months = []
    for i in range(5, -1, -1):
        mo = date(today.year, today.month, 1)
        for _ in range(i): mo = (mo - timedelta(days=1)).replace(day=1)
        total = float(qone("SELECT COALESCE(SUM(amount),0) s FROM society_expenses WHERE society_id=? AND expense_month LIKE ?", (sid, mo.strftime("%Y-%m")+"%"))["s"])
        months.append({"month": mo.strftime("%b %Y"), "month_key": mo.isoformat(), "total": total})
    ytd_start = f"{today.year}-01-01"
    ytd = float(qone("SELECT COALESCE(SUM(amount),0) s FROM society_expenses WHERE society_id=? AND expense_month>=?", (sid, ytd_start))["s"])
    return ok({"monthly_trend": months, "ytd_total": ytd})

@app.route("/api/expenses", methods=["POST"])
@admin_only
def add_expense():
    d = request.json or {}
    if not d.get("title"):  return err("title required")
    if not d.get("amount"): return err("amount required")
    if not d.get("category"): return err("category required")
    cat = d["category"].upper()
    if cat not in EXPENSE_CATEGORIES: return err(f"Category must be one of: {EXPENSE_CATEGORIES}")
    sid = int(d.get("society_id", 1))
    today = datetime.utcnow()
    month_str = d.get("expense_month", f"{today.year}-{today.month:02d}-01")
    if len(month_str) == 7: month_str += "-01"
    eid = qrun(
        "INSERT INTO society_expenses(society_id,expense_month,category,title,amount,vendor,invoice_number,payment_mode,notes,added_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, month_str, cat, d["title"], float(d["amount"]),
         d.get("vendor"), d.get("invoice_number"), d.get("payment_mode","CASH"),
         d.get("notes"), g.uid, _now(), _now()))
    return ok(r2d(qone("SELECT e.*,u.name added_by_name FROM society_expenses e LEFT JOIN users u ON u.id=e.added_by WHERE e.id=?", (eid,))))

@app.route("/api/expenses/<int:eid>", methods=["PUT"])
@admin_only
def update_expense(eid):
    d = request.json or {}
    fields=[]; vals=[]
    for f in ["title","amount","vendor","invoice_number","payment_mode","notes","category"]:
        if f in d: fields.append(f"{f}=?"); vals.append(d[f])
    if fields: vals.extend([_now(), eid]); qrun(f"UPDATE society_expenses SET {','.join(fields)},updated_at=? WHERE id=?", vals)
    return ok(r2d(qone("SELECT * FROM society_expenses WHERE id=?", (eid,))))

@app.route("/api/expenses/<int:eid>", methods=["DELETE"])
@admin_only
def delete_expense(eid):
    qrun("DELETE FROM society_expenses WHERE id=?", (eid,))
    return ok({"deleted": True})

@app.route("/api/expenses/categories", methods=["GET"])
def get_expense_categories():
    return ok(EXPENSE_CATEGORIES)

# ══════════════════════════════════════════════════════════════════════════════
# PLAN MANAGEMENT  (FREE / PAID subscription at society level)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/plans/features", methods=["GET"])
def get_plan_features():
    """Public: what each plan includes — useful for upgrade screens."""
    return ok({
        "plans": {
            "FREE": {"name":"Free","price":0,   "features":PLAN_FEATURES["FREE"]},
            "PAID": {"name":"Pro", "price":999, "features":ALL_PAID_FEATURES},
        },
        "feature_labels": {
            "flats":           "Flat & Resident Management",
            "visitors":        "Visitor Logging & Approval",
            "announcements":   "Society Announcements",
            "feedback":        "Complaints & Feedback",
            "guards":          "Guard Management",
            "dashboard_basic": "Basic Dashboard",
            "profile":         "User Profile",
            "maintenance":     "Maintenance Bill Generation",
            "payments":        "Online Payment Collection",
            "bank_account":    "Bank Account Management",
            "payment_history": "Payment History & Receipts",
            "dashboard_full":  "Full Analytics Dashboard",
            "service_directory":"Service Provider Directory (Electrician, Plumber, etc.)",
            "service_requests": "Service Booking & Appointments",
            "reports":         "Financial Reports (coming soon)",
            "bulk_sms":        "Bulk SMS to Residents (coming soon)",
            "custom_branding": "Society Branding (coming soon)",
            "api_access":      "API Access (coming soon)",
        }
    })

@app.route("/api/plans/<int:sid>", methods=["GET"])
@auth_required
def get_society_plan_detail(sid):
    """Get current plan, request status, and per-feature access map."""
    plan = _get_society_plan(sid)
    if plan.get("requested_by"):
        u = qone("SELECT name FROM users WHERE id=?", (plan["requested_by"],))
        plan["requested_by_name"] = u["name"] if u else None
    if plan.get("reviewed_by"):
        u = qone("SELECT name FROM users WHERE id=?", (plan["reviewed_by"],))
        plan["reviewed_by_name"] = u["name"] if u else None
    plan["can_use"] = {f: _society_can(sid, f) for f in PLAN_FEATURES["FREE"] + PLAN_FEATURES["PAID"]}
    return ok(plan)

@app.route("/api/plans/<int:sid>/request", methods=["POST"])
@auth_required
def request_plan_change(sid):
    """
    Any SOCIETY_ADMIN requests upgrade or downgrade.
    SUPER_ADMIN approves instantly.
    Regular admin request goes PENDING → same admin gives final approval.
    """
    if g.role not in ("SOCIETY_ADMIN","SUPER_ADMIN"):
        return err("Only Society Admin can request plan changes", 403)
    d = request.json or {}
    new_plan = (d.get("plan") or "").upper()
    if new_plan not in ("FREE","PAID"):
        return err("Plan must be FREE or PAID")
    plan = _get_society_plan(sid)
    if plan["current_plan"] == new_plan:
        return err(f"Society is already on the {new_plan} plan")
    if plan["request_status"] == "PENDING":
        return err("A plan change request is already pending approval.")
    # SUPER_ADMIN: instant activation
    if g.role == "SUPER_ADMIN":
        qrun("INSERT OR REPLACE INTO society_plans(society_id,current_plan,requested_plan,request_status,requested_by,requested_at,reviewed_by,reviewed_at,review_note,plan_activated_at,created_at,updated_at) VALUES(?,?,NULL,'NONE',?,?,?,?,'Approved by Super Admin',?,?,?)",
             (sid,new_plan,g.uid,_now(),g.uid,_now(),_now(),_now(),_now()))
        return ok({"message":f"Plan changed to {new_plan} (Super Admin)","current_plan":new_plan})
    # Society Admin: create PENDING request
    qrun("INSERT OR REPLACE INTO society_plans(society_id,current_plan,requested_plan,request_status,requested_by,requested_at,reviewed_by,reviewed_at,review_note,plan_activated_at,created_at,updated_at) VALUES(?,?,?,'PENDING',?,?,NULL,NULL,NULL,NULL,?,?)",
         (sid,plan["current_plan"],new_plan,g.uid,_now(),_now(),_now()))
    return ok({"message":f"Plan change to {new_plan} requested. Please review and give final approval.",
               "current_plan":plan["current_plan"],"requested_plan":new_plan,"request_status":"PENDING"})

@app.route("/api/plans/<int:sid>/approve", methods=["PUT"])
@admin_only
def approve_plan_change(sid):
    """Final approval by Society Admin (same society) or Super Admin."""
    plan = _get_society_plan(sid)
    if plan["request_status"] != "PENDING":
        return err("No pending plan change request found")
    if g.role != "SUPER_ADMIN":
        mem = qone("SELECT role FROM user_societies WHERE user_id=? AND society_id=? AND is_active=1",(g.uid,sid))
        if not mem or mem["role"] != "SOCIETY_ADMIN":
            return err("Only this society's admin can approve the plan change", 403)
    d = request.json or {}
    note = d.get("note","Approved by Society Admin")
    new_plan = plan["requested_plan"]
    qrun("UPDATE society_plans SET current_plan=?,requested_plan=NULL,request_status='APPROVED',reviewed_by=?,reviewed_at=?,review_note=?,plan_activated_at=?,updated_at=? WHERE society_id=?",
         (new_plan,g.uid,_now(),note,_now(),_now(),sid))
    return ok({"message":f"Plan {'upgraded to PAID ✅' if new_plan=='PAID' else 'downgraded to FREE'}","current_plan":new_plan})

@app.route("/api/plans/<int:sid>/reject", methods=["PUT"])
@admin_only
def reject_plan_change(sid):
    """Society Admin rejects the pending plan request."""
    plan = _get_society_plan(sid)
    if plan["request_status"] != "PENDING":
        return err("No pending plan change request found")
    if g.role != "SUPER_ADMIN":
        mem = qone("SELECT role FROM user_societies WHERE user_id=? AND society_id=? AND is_active=1",(g.uid,sid))
        if not mem or mem["role"] != "SOCIETY_ADMIN":
            return err("Only this society's admin can reject the plan change", 403)
    d = request.json or {}
    qrun("UPDATE society_plans SET requested_plan=NULL,request_status='REJECTED',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE society_id=?",
         (g.uid,_now(),d.get("note","Rejected"),_now(),sid))
    return ok({"message":"Plan change request rejected.","current_plan":plan["current_plan"]})

@app.route("/api/plans/<int:sid>/cancel", methods=["PUT"])
@auth_required
def cancel_plan_request(sid):
    plan = _get_society_plan(sid)
    if plan["request_status"] != "PENDING": return err("No pending request to cancel")
    if plan.get("requested_by") != g.uid and g.role not in ("SUPER_ADMIN","SOCIETY_ADMIN"):
        return err("Cannot cancel this request", 403)
    qrun("UPDATE society_plans SET requested_plan=NULL,request_status='NONE',updated_at=? WHERE society_id=?",(_now(),sid))
    return ok({"message":"Request cancelled."})

# ══════════════════════════════════════════════════════════════════════════════
# SUPER_ADMIN — platform-wide management
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/superadmin/stats", methods=["GET"])
@super_only
def sa_platform_stats():
    total_users      = qone("SELECT COUNT(*) c FROM users WHERE is_active=1")["c"]
    total_societies  = qone("SELECT COUNT(*) c FROM societies WHERE is_active=1")["c"]
    total_flats      = qone("SELECT COUNT(*) c FROM flats WHERE is_active=1")["c"]
    total_payments   = float(qone("SELECT COALESCE(SUM(total_amount),0) s FROM payments WHERE status='SUCCESS'")["s"])
    total_visitors   = qone("SELECT COUNT(*) c FROM visitor_logs")["c"]
    open_complaints  = qone("SELECT COUNT(*) c FROM feedbacks WHERE status='OPEN'")["c"]
    by_role = {r["role"]: r["c"] for r in qall("SELECT role,COUNT(*) c FROM users WHERE is_active=1 GROUP BY role")}
    societies = r2l(qall(
        "SELECT s.*,"
        "(SELECT COUNT(*) FROM flats f WHERE f.society_id=s.id AND f.is_active=1) active_flats,"
        "(SELECT COALESCE(SUM(total_amount),0) FROM payments p WHERE p.society_id=s.id AND p.status='SUCCESS') revenue "
        "FROM societies s WHERE s.is_active=1 ORDER BY s.name"))
    return ok({"total_users":total_users,"total_societies":total_societies,
               "total_flats":total_flats,"total_payments":total_payments,
               "total_visitors":total_visitors,"open_complaints":open_complaints,
               "users_by_role":by_role,"societies":societies})

@app.route("/api/superadmin/users", methods=["GET"])
@super_only
def sa_all_users():
    page = int(request.args.get("page",1))
    role = request.args.get("role")
    sql  = ("SELECT u.*,us.society_id,s.name society_name FROM users u "
            "LEFT JOIN user_societies us ON us.user_id=u.id AND us.is_active=1 "
            "LEFT JOIN societies s ON s.id=us.society_id WHERE 1=1")
    params=[]
    if role: sql+=" AND u.role=?"; params.append(role)
    sql+=" ORDER BY u.id"
    items, meta = paginate(r2l(qall(sql, params)), page, 30)
    return ok(items, meta=meta)

@app.route("/api/superadmin/users/<int:uid>/role", methods=["PUT"])
@super_only
def sa_change_role(uid):
    d = request.json or {}
    role = d.get("role","")
    valid = ("SUPER_ADMIN","SOCIETY_ADMIN","OWNER","TENANT","GUARD","MANAGER")
    if role not in valid: return err(f"Role must be one of {valid}")
    qrun("UPDATE users SET role=?,updated_at=? WHERE id=?", (role,_now(),uid))
    return ok(r2d(qone("SELECT * FROM users WHERE id=?",(uid,))))

@app.route("/api/superadmin/users/<int:uid>/toggle", methods=["PUT"])
@super_only
def sa_toggle_user(uid):
    u = qone("SELECT * FROM users WHERE id=?",(uid,))
    if not u: return err("Not found",404)
    new = 0 if u["is_active"] else 1
    qrun("UPDATE users SET is_active=?,updated_at=? WHERE id=?",(new,_now(),uid))
    return ok({"id":uid,"is_active":new})

@app.route("/api/superadmin/societies/<int:sid>/toggle", methods=["PUT"])
@super_only
def sa_toggle_society(sid):
    s = qone("SELECT * FROM societies WHERE id=?",(sid,))
    if not s: return err("Not found",404)
    new = 0 if s["is_active"] else 1
    qrun("UPDATE societies SET is_active=?,updated_at=? WHERE id=?",(new,_now(),sid))
    return ok({"id":sid,"is_active":new})


# ══════════════════════════════════════════════════════════════════════════════
# AI CHAT ENDPOINT  (proxies to Anthropic Claude API)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/chat", methods=["POST"])
@auth_required
def ai_chat():
    """Proxy the chatbot request to Claude API server-side."""
    import urllib.request, json as _json
    d = request.json or {}
    messages = d.get("messages", [])
    system   = d.get("system", "You are a helpful society management assistant.")
    if not messages: return err("messages required")
    # Trim to last 8 turns
    messages = messages[-8:]
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Graceful fallback — let frontend handle with rule-based answers
        return ok({"reply": None, "fallback": True}, "API key not configured")
    try:
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 400,
            "system": system,
            "messages": messages
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            rdata = _json.loads(resp.read())
        reply = ""
        for block in rdata.get("content", []):
            if block.get("type") == "text":
                reply += block["text"]
        return ok({"reply": reply.strip()})
    except Exception as e:
        log.warning(f"Claude API error: {e}")
        return ok({"reply": None, "fallback": True}, "AI unavailable, using offline answers")

# ══════════════════════════════════════════════════════════════════════════════
# FRONTEND
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/", defaults={"path":""})
@app.route("/<path:path>")
def serve(path):
    if path.startswith("api/"): return err("Not found",404)
    fp = os.path.join(STATIC, path)
    if path and os.path.exists(fp): return send_from_directory(STATIC, path)
    return send_from_directory(STATIC, "index.html")

# ══════════════════════════════════════════════════════════════════════════════
# DB INIT
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA="""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,mobile TEXT UNIQUE NOT NULL,name TEXT,email TEXT,role TEXT DEFAULT 'OWNER',is_active INTEGER DEFAULT 1,fcm_token TEXT,created_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS societies(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,address TEXT,city TEXT,state TEXT,pincode TEXT,registration_number TEXT,total_flats INTEGER DEFAULT 0,maintenance_due_day INTEGER DEFAULT 5,is_active INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS society_plans(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER UNIQUE NOT NULL,current_plan TEXT DEFAULT 'FREE',requested_plan TEXT,request_status TEXT DEFAULT 'NONE',requested_by INTEGER,requested_at TEXT,reviewed_by INTEGER,reviewed_at TEXT,review_note TEXT,plan_activated_at TEXT,plan_expires_at TEXT,created_at TEXT,updated_at TEXT,FOREIGN KEY(society_id) REFERENCES societies(id));
CREATE INDEX IF NOT EXISTS ix_sp_soc ON society_plans(society_id);
CREATE TABLE IF NOT EXISTS user_societies(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,society_id INTEGER,role TEXT,is_active INTEGER DEFAULT 1,FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(society_id) REFERENCES societies(id));
CREATE INDEX IF NOT EXISTS ix_us_user ON user_societies(user_id);
CREATE TABLE IF NOT EXISTS wings(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,name TEXT NOT NULL,total_floors INTEGER DEFAULT 10,FOREIGN KEY(society_id) REFERENCES societies(id));
CREATE TABLE IF NOT EXISTS flats(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,wing_id INTEGER,flat_number TEXT NOT NULL,floor INTEGER,flat_type TEXT,area_sqft REAL,monthly_maintenance REAL DEFAULT 3500,status TEXT DEFAULT 'VACANT',is_active INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT,FOREIGN KEY(society_id) REFERENCES societies(id));
CREATE INDEX IF NOT EXISTS ix_flat_soc ON flats(society_id);
CREATE TABLE IF NOT EXISTS flat_owners(id INTEGER PRIMARY KEY AUTOINCREMENT,flat_id INTEGER,user_id INTEGER,is_primary INTEGER DEFAULT 1,ownership_start TEXT,is_active INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_fo_user ON flat_owners(user_id);
CREATE TABLE IF NOT EXISTS tenants(id INTEGER PRIMARY KEY AUTOINCREMENT,flat_id INTEGER,user_id INTEGER,owner_id INTEGER,lease_start TEXT,lease_end TEXT,monthly_rent REAL,is_active INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_ten_user ON tenants(user_id);
CREATE TABLE IF NOT EXISTS maintenance_bills(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,flat_id INTEGER,bill_month TEXT,base_amount REAL,penalty_amount REAL DEFAULT 0,total_amount REAL,due_date TEXT,status TEXT DEFAULT 'PENDING',paid_at TEXT,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_bill_soc ON maintenance_bills(society_id,bill_month);
CREATE INDEX IF NOT EXISTS ix_bill_flat ON maintenance_bills(flat_id);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,receipt_number TEXT UNIQUE,society_id INTEGER,flat_id INTEGER,paid_by INTEGER,plan_type TEXT,period_start TEXT,period_end TEXT,base_amount REAL,discount_amount REAL DEFAULT 0,processing_fee REAL DEFAULT 0,total_amount REAL,payment_method TEXT,upi_app TEXT,card_last4 TEXT,pg_order_id TEXT,pg_payment_id TEXT,status TEXT DEFAULT 'SUCCESS',completed_at TEXT,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_pay_soc ON payments(society_id);
CREATE INDEX IF NOT EXISTS ix_pay_flat ON payments(flat_id);
CREATE TABLE IF NOT EXISTS society_bank_accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER UNIQUE,account_holder TEXT,account_number TEXT,ifsc_code TEXT,bank_name TEXT,branch TEXT,account_type TEXT DEFAULT 'SAVINGS',is_verified INTEGER DEFAULT 0,verified_at TEXT,linked_by INTEGER,created_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS visitor_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,flat_id INTEGER,guard_id INTEGER,visitor_name TEXT,visitor_mobile TEXT,vehicle_number TEXT,purpose TEXT,visit_type TEXT DEFAULT 'GUEST',status TEXT DEFAULT 'PENDING',approved_by INTEGER,approved_at TEXT,entry_time TEXT,exit_time TEXT,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_vis_soc ON visitor_logs(society_id);
CREATE INDEX IF NOT EXISTS ix_vis_flat ON visitor_logs(flat_id);
CREATE TABLE IF NOT EXISTS guards(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,society_id INTEGER,employee_id TEXT,shift TEXT DEFAULT 'MORNING',gate_assigned TEXT,is_active INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_guard_user ON guards(user_id);
CREATE TABLE IF NOT EXISTS feedbacks(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,flat_id INTEGER,submitted_by INTEGER,category TEXT,title TEXT,description TEXT,priority TEXT DEFAULT 'MEDIUM',status TEXT DEFAULT 'OPEN',assigned_to INTEGER,resolved_at TEXT,rating INTEGER,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_fb_soc ON feedbacks(society_id,status);
CREATE TABLE IF NOT EXISTS announcements(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,title TEXT,content TEXT,category TEXT DEFAULT 'GENERAL',is_pinned INTEGER DEFAULT 0,published_by INTEGER,expires_at TEXT,created_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS service_providers(id INTEGER PRIMARY KEY AUTOINCREMENT,provider_code TEXT UNIQUE,name TEXT NOT NULL,mobile TEXT,email TEXT,category TEXT NOT NULL,specializations TEXT,experience_years INTEGER DEFAULT 0,hourly_rate REAL,bio TEXT,id_proof_type TEXT,id_proof_number TEXT,verification_status TEXT DEFAULT 'PENDING',is_active INTEGER DEFAULT 1,is_blocked INTEGER DEFAULT 0,block_reason TEXT,rating_avg REAL DEFAULT 0,rating_count INTEGER DEFAULT 0,positive_count INTEGER DEFAULT 0,negative_count INTEGER DEFAULT 0,auto_flag INTEGER DEFAULT 0,requested_by INTEGER,approved_by INTEGER,approved_at TEXT,rejection_note TEXT,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_sp_cat ON service_providers(category,verification_status);
CREATE INDEX IF NOT EXISTS ix_sp_mobile ON service_providers(mobile);
CREATE TABLE IF NOT EXISTS provider_society_links(id INTEGER PRIMARY KEY AUTOINCREMENT,provider_id INTEGER,society_id INTEGER,is_active INTEGER DEFAULT 1,added_by INTEGER,added_at TEXT,FOREIGN KEY(provider_id) REFERENCES service_providers(id),FOREIGN KEY(society_id) REFERENCES societies(id));
CREATE UNIQUE INDEX IF NOT EXISTS ix_psl_uniq ON provider_society_links(provider_id,society_id);
CREATE TABLE IF NOT EXISTS service_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER,flat_id INTEGER,requested_by INTEGER,provider_id INTEGER,service_category TEXT,description TEXT,preferred_date TEXT,preferred_time TEXT,status TEXT DEFAULT 'PENDING',confirmed_at TEXT,scheduled_date TEXT,scheduled_time TEXT,completion_note TEXT,completed_at TEXT,cancelled_at TEXT,cancel_reason TEXT,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_sr_soc ON service_requests(society_id,status);
CREATE INDEX IF NOT EXISTS ix_sr_flat ON service_requests(flat_id);
CREATE INDEX IF NOT EXISTS ix_sr_prov ON service_requests(provider_id);
CREATE TABLE IF NOT EXISTS service_ratings(id INTEGER PRIMARY KEY AUTOINCREMENT,request_id INTEGER UNIQUE,provider_id INTEGER,flat_id INTEGER,society_id INTEGER,rated_by INTEGER,rating_type TEXT NOT NULL,rating_score INTEGER DEFAULT 5,comment TEXT,created_at TEXT,FOREIGN KEY(request_id) REFERENCES service_requests(id),FOREIGN KEY(provider_id) REFERENCES service_providers(id));
CREATE INDEX IF NOT EXISTS ix_srat_prov ON service_ratings(provider_id,rating_type);
CREATE TABLE IF NOT EXISTS society_expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,society_id INTEGER NOT NULL,expense_month TEXT NOT NULL,category TEXT NOT NULL,title TEXT NOT NULL,amount REAL NOT NULL,vendor TEXT,invoice_number TEXT,payment_mode TEXT DEFAULT 'CASH',notes TEXT,added_by INTEGER,created_at TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_exp_soc ON society_expenses(society_id,expense_month);
"""

def _n2(): return datetime.utcnow().isoformat(sep=" ")

def seed(db):
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]>0: return
    log.info("🌱  Seeding demo data …")
    for uid,mob,name,email,role in [
        (1,"9999000000","Super Admin","superadmin@sp.com","SUPER_ADMIN"),
        (2,"9999000001","Rajesh Agarwal","admin@sp.com","SOCIETY_ADMIN"),
        (3,"9999000002","Suresh Mehta","suresh@sp.com","OWNER"),
        (4,"9999000003","Priya Sharma","priya@sp.com","OWNER"),
        (5,"9999000004","Anil Joshi","anil@sp.com","OWNER"),
        (6,"9999000005","Kavita Desai","kavita@sp.com","OWNER"),
        (7,"9999000006","Ravi Kumar","ravi@sp.com","TENANT"),
        (8,"9999000007","Sunita Rao","sunita@sp.com","TENANT"),
        (9,"9999000008","Ramesh Yadav","guard1@sp.com","GUARD"),
        (10,"9999000009","Vikram Nair","vikram@sp.com","OWNER"),
    ]:
        db.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,1,NULL,?,?)",(uid,mob,name,email,role,_n2(),_n2()))

    db.execute("INSERT OR IGNORE INTO societies VALUES(1,'Greenview Heights','Plot 14, Baner Road','Pune','Maharashtra','411045','MH/PNE/CHS/14521',120,5,1,?,?)",(_n2(),_n2()))
    for row in [(1,1,"SUPER_ADMIN"),(2,1,"SOCIETY_ADMIN"),(3,1,"OWNER"),(4,1,"OWNER"),(5,1,"OWNER"),(6,1,"OWNER"),(7,1,"TENANT"),(8,1,"TENANT"),(9,1,"GUARD"),(10,1,"OWNER")]:
        db.execute("INSERT OR IGNORE INTO user_societies(user_id,society_id,role,is_active) VALUES(?,?,?,1)",row)

    for i,wn in enumerate(["Wing A","Wing B","Wing C","Wing D"],1):
        db.execute("INSERT OR IGNORE INTO wings VALUES(?,1,?,10)",(i,wn))

    for fid,wid,fn,fl,ft,area,maint,st in [
        (1,1,"A-101",1,"2BHK",850,3500,"OWNER_OCCUPIED"),
        (2,1,"A-201",2,"3BHK",1200,5000,"TENANT_OCCUPIED"),
        (3,1,"A-401",4,"2BHK",850,3500,"OWNER_OCCUPIED"),
        (4,2,"B-102",1,"2BHK",850,3500,"OWNER_OCCUPIED"),
        (5,2,"B-301",3,"1BHK",550,2500,"TENANT_OCCUPIED"),
        (6,3,"C-204",2,"3BHK",1200,5000,"VACANT"),
        (7,4,"D-401",4,"4BHK",1800,7500,"OWNER_OCCUPIED"),
    ]:
        db.execute("INSERT OR IGNORE INTO flats VALUES(?,1,?,?,?,?,?,?,?,1,?,?)",(fid,wid,fn,fl,ft,area,maint,st,_n2(),_n2()))

    for fid,uid in [(1,3),(2,4),(3,5),(4,6),(7,10)]:
        db.execute("INSERT OR IGNORE INTO flat_owners(flat_id,user_id,is_primary,is_active,created_at,updated_at) VALUES(?,?,1,1,?,?)",(fid,uid,_n2(),_n2()))
    fo=db.execute("SELECT id FROM flat_owners WHERE flat_id=2 AND user_id=4").fetchone()
    if fo: db.execute("INSERT OR IGNORE INTO tenants(flat_id,user_id,owner_id,lease_start,lease_end,monthly_rent,is_active,created_at,updated_at) VALUES(2,7,?,'2025-01-01','2025-12-31',22000,1,?,?)",(fo[0],_n2(),_n2()))
    fo2=db.execute("SELECT id FROM flat_owners WHERE flat_id=5 AND user_id=6").fetchone()
    if fo2: db.execute("INSERT OR IGNORE INTO tenants(flat_id,user_id,owner_id,lease_start,lease_end,monthly_rent,is_active,created_at,updated_at) VALUES(5,8,?,'2025-03-01','2026-02-28',12000,1,?,?)",(fo2[0],_n2(),_n2()))

    today=date.today()
    for mi in range(2,-1,-1):
        mo=date(today.year,today.month,1)
        for _ in range(mi): mo=(mo-timedelta(days=1)).replace(day=1)
        due=date(mo.year,mo.month,5)
        for fid,maint in [(1,3500),(2,5000),(3,3500),(4,3500),(5,2500),(7,7500)]:
            st="PAID" if mi>0 else ("PENDING" if fid in [2,5] else "PAID")
            paid_at=f"{mo.year}-{mo.month:02d}-03 10:00:00" if st=="PAID" else None
            db.execute("INSERT OR IGNORE INTO maintenance_bills(society_id,flat_id,bill_month,base_amount,penalty_amount,total_amount,due_date,status,paid_at,created_at,updated_at) VALUES(1,?,?,?,0,?,?,?,?,?,?)",(fid,mo.isoformat(),maint,maint,due.isoformat(),st,paid_at,_n2(),_n2()))

    db.execute("INSERT OR IGNORE INTO payments(receipt_number,society_id,flat_id,paid_by,plan_type,period_start,period_end,base_amount,discount_amount,processing_fee,total_amount,payment_method,card_last4,status,completed_at,created_at,updated_at) VALUES('RCP-1-20260403-000001',1,1,2,'MONTHLY','2026-04-01','2026-04-30',3500,0,52.5,3552.5,'CREDIT_CARD','4532','SUCCESS',?,?,?)",(_n2(),_n2(),_n2()))
    db.execute("INSERT OR IGNORE INTO payments(receipt_number,society_id,flat_id,paid_by,plan_type,period_start,period_end,base_amount,discount_amount,processing_fee,total_amount,payment_method,card_last4,status,completed_at,created_at,updated_at) VALUES('RCP-1-20250103-000002',1,2,3,'YEARLY','2025-01-01','2025-12-31',60000,6000,810,54810,'CREDIT_CARD','7823','SUCCESS',?,?,?)",(_n2(),_n2(),_n2()))

    for vname,vmob,fid,vtype,vstatus in [
        ("Deepak Kumar","9876500001",1,"GUEST","INSIDE"),
        ("Swiggy Delivery","9876500002",4,"DELIVERY","EXITED"),
        ("Meena Raut","9876500003",5,"GUEST","PENDING"),
        ("Ajay Plumber","9876500004",7,"VENDOR","PENDING"),
        ("Priya's Mother","9876500005",2,"GUEST","PENDING"),
    ]:
        db.execute("INSERT OR IGNORE INTO visitor_logs(society_id,flat_id,visitor_name,visitor_mobile,visit_type,status,created_at,updated_at) VALUES(1,?,?,?,?,?,?,?)",(fid,vname,vmob,vtype,vstatus,_n2(),_n2()))

    for cat,title,pri,st in [
        ("LIFT","Lift not working – Wing B","HIGH","OPEN"),
        ("WATER","Water pressure low – 4th floor","MEDIUM","IN_PROGRESS"),
        ("ELECTRICAL","Parking lights not working","LOW","RESOLVED"),
        ("SECURITY","Security camera offline – Gate 2","HIGH","OPEN"),
    ]:
        db.execute("INSERT OR IGNORE INTO feedbacks(society_id,flat_id,submitted_by,category,title,priority,status,created_at,updated_at) VALUES(1,1,2,?,?,?,?,?,?)",(cat,title,pri,st,_n2(),_n2()))

    for title,content,cat,pin in [
        ("Water tank cleaning – Apr 20","No water supply 9AM–2PM on April 20th. Please store water.","NOTICE",1),
        ("Society AGM – Apr 28 6PM","Annual General Meeting at Clubhouse. All owners must attend.","EVENT",0),
        ("New visitor entry rules from May 1","All visitors must register at gate with photo ID from May 1st.","GENERAL",0),
        ("Maintenance due – April","April maintenance is due by 5th. Pay online or at office.","NOTICE",0),
    ]:
        db.execute("INSERT OR IGNORE INTO announcements(society_id,title,content,category,is_pinned,published_by,created_at,updated_at) VALUES(1,?,?,?,?,1,?,?)",(title,content,cat,pin,_n2(),_n2()))

    db.execute("INSERT OR IGNORE INTO guards(user_id,society_id,employee_id,shift,gate_assigned,is_active,created_at,updated_at) VALUES(8,1,'GRD-001','MORNING','Main Gate',1,?,?)",(_n2(),_n2()))
    db.execute("INSERT OR IGNORE INTO society_bank_accounts(society_id,account_holder,account_number,ifsc_code,bank_name,account_type,is_verified,verified_at,linked_by,created_at,updated_at) VALUES(1,'Greenview Heights CHS','123456784821','HDFC0002341','HDFC Bank','SAVINGS',1,?,1,?,?)",(_n2(),_n2(),_n2()))
    db.commit()
    log.info("✅  Demo data seeded!")

def init_db():
    db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    db.row_factory = sqlite3.Row
    for s in SCHEMA.split(";"):
        s=s.strip()
        if s: db.execute(s)
    db.commit(); seed(db); db.close()
    log.info("✅  DB ready → %s", DB_PATH)

if __name__ == "__main__":
    os.makedirs(STATIC, exist_ok=True)
    init_db()
    port = int(CFG["app"]["port"])
    log.info("\n"+"="*56+
        f"\n🏘️   SocietyPro  →  http://localhost:{port}"+
        "\n\n  DEMO LOGINS  (mobile → OTP printed in this console)"+
        "\n  9999000000  Super Admin     SUPER_ADMIN  (all access)"+
        "\n  9999000001  Rajesh Agarwal  SOCIETY_ADMIN"+
        "\n  9999000002  Suresh Mehta    OWNER  (Flat A-101)"+
        "\n  9999000003  Priya Sharma    OWNER  (Flat A-201)"+
        "\n  9999000006  Ravi Kumar      TENANT (Flat A-201)"+
        "\n  9999000007  Sunita Rao      TENANT (Flat B-301)"+
        "\n  9999000008  Ramesh Yadav    GUARD  (Main Gate)"+
        "\n"+"="*56)
    app.run(host="0.0.0.0", port=port, debug=False)
