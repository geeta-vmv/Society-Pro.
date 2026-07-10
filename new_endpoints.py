"""
SocietyPro Backend - Additional Endpoints for Owner/Tenant Fixes
Add these routes to app.py after the existing endpoints
"""

# ══════════════════════════════════════════════════════════════════════════════
# RENT MANAGEMENT (New Feature)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/rent/bills", methods=["GET"])
@auth_required
def get_rent_bills():
    """Get rent bills - owner sees all their tenants' bills, tenant sees their own"""
    sid = int(sid) if sid and str(sid).isdigit() else _get_user_society(g.uid)
    page = int(page)if str(page).isdigit() else 1
    
    if g.role == "TENANT":
        # Tenant sees only their rent bills
        tenant = qone("SELECT id FROM tenants WHERE user_id=? AND is_active=1", (g.uid,))
        if not tenant:
            return ok([])
        bills = r2l(qall(
            "SELECT rb.*, f.flat_number, o.name as owner_name "
            "FROM rent_bills rb "
            "JOIN flats f ON f.id=rb.flat_id "
            "JOIN flat_owners fo ON fo.id=rb.owner_id "
            "JOIN users o ON o.id=fo.user_id "
            "WHERE rb.tenant_id=? ORDER BY rb.bill_month DESC",
            (tenant["id"],)
        ))
    elif g.role == "OWNER":
        # Owner sees all rent bills for their tenants
        owner = qone("SELECT id FROM flat_owners WHERE user_id=? AND is_primary=1", (g.uid,))
        if not owner:
            return ok([])
        bills = r2l(qall(
            "SELECT rb.*, t.user_id as tenant_user_id, u.name as tenant_name, f.flat_number "
            "FROM rent_bills rb "
            "JOIN tenants t ON t.id=rb.tenant_id "
            "JOIN users u ON u.id=t.user_id "
            "JOIN flats f ON f.id=rb.flat_id "
            "WHERE rb.owner_id=? ORDER BY rb.bill_month DESC",
            (owner["id"],)
        ))
    else:
        # Admin sees all rent bills for the society
        bills = r2l(qall(
            "SELECT rb.*, t.user_id as tenant_user_id, u.name as tenant_name, "
            "f.flat_number, o.name as owner_name "
            "FROM rent_bills rb "
            "JOIN tenants t ON t.id=rb.tenant_id "
            "JOIN users u ON u.id=t.user_id "
            "JOIN flats f ON f.id=rb.flat_id "
            "JOIN flat_owners fo ON fo.id=rb.owner_id "
            "JOIN users o ON o.id=fo.user_id "
            "WHERE f.society_id=? ORDER BY rb.bill_month DESC",
            (sid,)
        ))
    
    data, meta = paginate(bills, page)
    return ok(data, meta=meta)


@app.route("/api/rent/generate", methods=["POST"])
@admin_only
def generate_rent_bills():
    """Generate rent bills for all active tenants for a given month"""
    d = request.json or {}
    sid = int(d.get("society_id", 1))
    bill_month = d.get("bill_month")  # Format: "2025-05"
    
    if not bill_month or len(bill_month) != 7:
        return err("bill_month required in format YYYY-MM")
    
    # Get all active tenants in this society
    tenants = r2l(qall(
        "SELECT t.*, f.society_id "
        "FROM tenants t "
        "JOIN flats f ON f.id=t.flat_id "
        "WHERE f.society_id=? AND t.is_active=1 AND t.lease_end >= ?",
        (sid, bill_month + "-01")
    ))
    
    created = 0
    for tenant in tenants:
        # Check if bill already exists
        exists = qone(
            "SELECT id FROM rent_bills WHERE tenant_id=? AND bill_month=?",
            (tenant["id"], bill_month + "-01")
        )
        if exists:
            continue
        
        # Calculate due date (5th of the month)
        due_date = f"{bill_month}-05"
        
        # Create rent bill
        qrun(
            "INSERT INTO rent_bills(tenant_id,flat_id,owner_id,bill_month,rent_amount,total_amount,due_date,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",
            (tenant["id"], tenant["flat_id"], tenant["owner_id"], 
             bill_month + "-01", tenant["monthly_rent"], tenant["monthly_rent"],
             due_date, _now(), _now())
        )
        created += 1
    
    return ok({"generated": created, "message": f"Created {created} rent bills for {bill_month}"})


@app.route("/api/rent/bills/<int:bid>/pay", methods=["POST"])
@auth_required
def pay_rent_bill(bid):
    """Tenant pays rent bill"""
    d = request.json or {}
    bill = qone("SELECT * FROM rent_bills WHERE id=?", (bid,))
    
    if not bill:
        return err("Bill not found", 404)
    
    if bill["status"] == "PAID":
        return err("Bill already paid")
    
    # Only tenant can pay their own rent
    if g.role == "TENANT":
        tenant = qone("SELECT id FROM tenants WHERE user_id=? AND id=?", (g.uid, bill["tenant_id"]))
        if not tenant:
            return err("Access denied", 403)
    elif g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN"):
        return err("Only tenant can pay rent", 403)
    
    # Generate receipt number
    flat = qone("SELECT society_id FROM flats WHERE id=?", (bill["flat_id"],))
    rno = f"RENT-{flat['society_id']}-{datetime.now().strftime('%Y%m%d')}-{''.join(random.choices(string.digits, k=6))}"
    
    # Create payment record
    pid = qrun(
        "INSERT INTO rent_payments(receipt_number,rent_bill_id,tenant_id,flat_id,owner_id,paid_by,amount,payment_method,upi_app,card_last4,status,completed_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rno, bid, bill["tenant_id"], bill["flat_id"], bill["owner_id"], g.uid,
         bill["total_amount"], d.get("payment_method"), d.get("upi_app"), 
         d.get("card_last4"), "SUCCESS", _now(), _now(), _now())
    )
    
    # Mark bill as paid
    qrun("UPDATE rent_bills SET status='PAID',paid_at=?,updated_at=? WHERE id=?", 
         (_now(), _now(), bid))
    
    # Log action
    _audit_log(g.uid, "RENT_PAID", "RENT_BILL", bid, None, rno)
    
    return ok({
        "payment": r2d(qone("SELECT * FROM rent_payments WHERE id=?", (pid,))),
        "receipt_number": rno
    })


@app.route("/api/rent/apply-penalties", methods=["POST"])
@admin_only
def apply_rent_penalties():
    """Apply penalties to overdue rent bills"""
    d = request.json or {}
    sid = int(d.get("society_id", 1))
    penalty_pct = float(d.get("penalty_pct", 2.0)) / 100
    
    today = date.today().isoformat()
    
    # Get overdue rent bills
    bills = r2l(qall(
        "SELECT rb.* FROM rent_bills rb "
        "JOIN flats f ON f.id=rb.flat_id "
        "WHERE f.society_id=? AND rb.status='PENDING' AND rb.due_date<?",
        (sid, today)
    ))
    
    updated = 0
    for bill in bills:
        penalty = round(float(bill["rent_amount"]) * penalty_pct, 2)
        total = float(bill["rent_amount"]) + penalty
        
        qrun(
            "UPDATE rent_bills SET status='OVERDUE',penalty_amount=?,total_amount=?,updated_at=? WHERE id=?",
            (penalty, total, _now(), bill["id"])
        )
        updated += 1
    
    return ok({"updated": updated, "message": f"Applied penalties to {updated} overdue rent bills"})


# ══════════════════════════════════════════════════════════════════════════════
# TENANT MANAGEMENT (Enhanced)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tenants/my-tenants", methods=["GET"])
@auth_required
def get_my_tenants():
    """Owner gets list of all their tenants across flats"""
    if g.role != "OWNER":
        return err("Only owners can access this", 403)
    
    owner = qone("SELECT id FROM flat_owners WHERE user_id=? AND is_primary=1", (g.uid,))
    if not owner:
        return ok([])
    
    tenants = r2l(qall(
        "SELECT t.*, u.name, u.mobile, u.email, f.flat_number, f.society_id, "
        "CAST((julianday(t.lease_end) - julianday('now')) AS INTEGER) as days_until_expiry, "
        "CASE WHEN t.lease_end < date('now') THEN 'EXPIRED' "
        "     WHEN julianday(t.lease_end) - julianday('now') <= 30 THEN 'EXPIRING_SOON' "
        "     ELSE 'ACTIVE' END as lease_status "
        "FROM tenants t "
        "JOIN users u ON u.id=t.user_id "
        "JOIN flats f ON f.id=t.flat_id "
        "WHERE t.owner_id=? AND t.is_active=1 "
        "ORDER BY t.lease_end ASC",
        (owner["id"],)
    ))
    
    return ok(tenants)


@app.route("/api/tenants/<int:tid>/owner-info", methods=["GET"])
@auth_required
def get_owner_info(tid):
    """Tenant gets their owner's contact information"""
    if g.role != "TENANT":
        return err("Only tenants can access this", 403)
    
    tenant = qone("SELECT * FROM tenants WHERE id=? AND user_id=?", (tid, g.uid))
    if not tenant:
        return err("Access denied", 403)
    
    owner = qone(
        "SELECT u.id, u.name, u.mobile, u.email "
        "FROM flat_owners fo "
        "JOIN users u ON u.id=fo.user_id "
        "WHERE fo.id=?",
        (tenant["owner_id"],)
    )
    
    return ok(r2d(owner))


@app.route("/api/tenants/<int:tid>/extend-lease", methods=["PUT"])
@auth_required
def extend_lease(tid):
    """Owner extends tenant's lease"""
    d = request.json or {}
    new_end = d.get("new_lease_end")
    
    if not new_end:
        return err("new_lease_end required")
    
    tenant = qone("SELECT * FROM tenants WHERE id=?", (tid,))
    if not tenant:
        return err("Tenant not found", 404)
    
    # Check if user is the owner
    if g.role == "OWNER":
        owner = qone("SELECT id FROM flat_owners WHERE user_id=? AND id=?", 
                     (g.uid, tenant["owner_id"]))
        if not owner:
            return err("Access denied - not your tenant", 403)
    elif g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN"):
        return err("Access denied", 403)
    
    old_end = tenant["lease_end"]
    qrun("UPDATE tenants SET lease_end=?,updated_at=? WHERE id=?", (new_end, _now(), tid))
    
    _audit_log(g.uid, "LEASE_EXTENDED", "TENANT", tid, old_end, new_end)
    
    return ok(r2d(qone("SELECT * FROM tenants WHERE id=?", (tid,))))


@app.route("/api/tenants/<int:tid>/deactivate", methods=["PUT"])
@auth_required
def deactivate_tenant(tid):
    """Owner or admin deactivates tenant (move-out complete)"""
    tenant = qone("SELECT * FROM tenants WHERE id=?", (tid,))
    if not tenant:
        return err("Tenant not found", 404)
    
    # Check permissions
    if g.role == "OWNER":
        owner = qone("SELECT id FROM flat_owners WHERE user_id=? AND id=?", 
                     (g.uid, tenant["owner_id"]))
        if not owner:
            return err("Access denied", 403)
    elif g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN"):
        return err("Access denied", 403)
    
    # Deactivate tenant
    qrun("UPDATE tenants SET is_active=0,updated_at=? WHERE id=?", (_now(), tid))
    
    # Update flat status
    qrun("UPDATE flats SET status='OWNER_OCCUPIED',updated_at=? WHERE id=?", 
         (_now(), tenant["flat_id"]))
    
    _audit_log(g.uid, "TENANT_DEACTIVATED", "TENANT", tid, "active", "inactive")
    
    return ok({"message": "Tenant deactivated successfully"})


# ══════════════════════════════════════════════════════════════════════════════
# LEASE EXPIRY AUTOMATION
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/check-expired-leases", methods=["POST"])
@admin_only
def check_expired_leases():
    """Check and handle expired leases (can be run as cron job)"""
    today = date.today().isoformat()
    
    # Find expired leases
    expired = r2l(qall(
        "SELECT t.*, u.name as tenant_name, u.mobile, f.flat_number, "
        "o.name as owner_name, o.mobile as owner_mobile "
        "FROM tenants t "
        "JOIN users u ON u.id=t.user_id "
        "JOIN flats f ON f.id=t.flat_id "
        "JOIN flat_owners fo ON fo.id=t.owner_id "
        "JOIN users o ON o.id=fo.user_id "
        "WHERE t.is_active=1 AND t.lease_end<?",
        (today,)
    ))
    
    results = {
        "expired_count": len(expired),
        "deactivated": [],
        "notifications_sent": []
    }
    
    for tenant in expired:
        # Auto-deactivate if expired more than 7 days ago
        days_expired = (date.today() - date.fromisoformat(tenant["lease_end"])).days
        
        if days_expired > 7:
            qrun("UPDATE tenants SET is_active=0,updated_at=? WHERE id=?", 
                 (_now(), tenant["id"]))
            qrun("UPDATE flats SET status='VACANT',updated_at=? WHERE id=?", 
                 (_now(), tenant["flat_id"]))
            results["deactivated"].append(tenant["id"])
            _audit_log(None, "LEASE_AUTO_EXPIRED", "TENANT", tenant["id"], 
                      tenant["lease_end"], "deactivated")
        else:
            # Send notification to owner and tenant
            # TODO: Implement actual notification system
            log.info(f"Lease expired for tenant {tenant['tenant_name']} "
                    f"in flat {tenant['flat_number']}")
            results["notifications_sent"].append(tenant["id"])
    
    # Update job status
    qrun("UPDATE system_jobs SET last_run_at=?,run_count=run_count+1,updated_at=? WHERE job_name='check_expired_leases'",
         (_now(), _now()))
    
    return ok(results)


@app.route("/api/tenants/expiring-soon", methods=["GET"])
@auth_required
def get_expiring_leases():
    """Get list of leases expiring in next 30 days"""
    sid = int(request.args.get("society_id", _get_user_society(g.uid)))
    days = int(request.args.get("days", 30))
    
    if g.role == "OWNER":
        owner = qone("SELECT id FROM flat_owners WHERE user_id=? AND is_primary=1", (g.uid,))
        if not owner:
            return ok([])
        where = "AND t.owner_id=?"
        params = (days, owner["id"])
    else:
        where = "AND f.society_id=?"
        params = (days, sid)
    
    expiring = r2l(qall(
        f"SELECT t.*, u.name as tenant_name, u.mobile, f.flat_number, "
        f"CAST((julianday(t.lease_end) - julianday('now')) AS INTEGER) as days_remaining "
        f"FROM tenants t "
        f"JOIN users u ON u.id=t.user_id "
        f"JOIN flats f ON f.id=t.flat_id "
        f"WHERE t.is_active=1 "
        f"AND julianday(t.lease_end) - julianday('now') BETWEEN 0 AND ? "
        f"{where} "
        f"ORDER BY t.lease_end ASC",
        params
    ))
    
    return ok(expiring)


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _audit_log(user_id, action, entity_type, entity_id, old_value, new_value):
    """Log important actions to audit trail"""
    try:
        qrun(
            "INSERT INTO audit_logs(user_id,action,entity_type,entity_id,old_value,new_value,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, action, entity_type, entity_id, str(old_value) if old_value else None,
             str(new_value) if new_value else None, _now())
        )
    except Exception as e:
        log.warning(f"Audit log failed: {e}")


def _get_current_flat_resident(flat_id):
    """Return the user_id of person currently living in flat (tenant if exists, else owner)"""
    today = date.today().isoformat()
    
    # Check for active tenant
    tenant = qone(
        "SELECT user_id FROM tenants WHERE flat_id=? AND is_active=1 AND lease_end>=?",
        (flat_id, today)
    )
    if tenant:
        return tenant["user_id"], "TENANT"
    
    # Otherwise, primary owner
    owner = qone(
        "SELECT user_id FROM flat_owners WHERE flat_id=? AND is_primary=1 AND is_active=1",
        (flat_id,)
    )
    if owner:
        return owner["user_id"], "OWNER"
    
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# MODIFICATIONS TO EXISTING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# REPLACE the existing /api/payments/initiate endpoint with this:
@app.route("/api/payments/initiate", methods=["POST"])
@auth_required
@plan_required("payments")
def initiate_payment():
    d = request.json or {}
    fid = int(d.get("flat_id", 0))
    flat = qone("SELECT * FROM flats WHERE id=?", (fid,))
    if not flat: return err("Flat not found", 404)
    
    # FIX: Check who should pay maintenance
    if g.role == "TENANT":
        tenant = qone(
            "SELECT pays_maintenance FROM tenants WHERE user_id=? AND flat_id=? AND is_active=1",
            (g.uid, fid)
        )
        if not tenant:
            return err("You are not associated with this flat", 403)
        if not tenant.get('pays_maintenance'):
            return err(
                "Maintenance payment is the owner's responsibility for this flat. "
                "Please contact your owner.",
                403
            )
    elif g.role == "OWNER":
        # Owner can pay
        my_flat = _get_user_flat(g.uid)
        if not my_flat or my_flat["id"] != fid:
            return err("Access denied", 403)
    elif g.role not in ("SOCIETY_ADMIN", "SUPER_ADMIN"):
        return err("Access denied", 403)
    
    plan = d.get("plan_type", "MONTHLY")
    fee_pct = Decimal(str(CFG["payment"]["processing_fee_pct"])) / 100
    disc_pct = Decimal(str(CFG["payment"]["yearly_discount_pct"])) / 100
    base = Decimal(str(flat["monthly_maintenance"] or 0))
    if plan == "YEARLY": base *= 12
    disc = (base * disc_pct).quantize(Decimal("0.01")) if plan == "YEARLY" else Decimal("0")
    after = base - disc
    fee = (after * fee_pct).quantize(Decimal("0.01"))
    total = after + fee
    
    return ok({
        "flat_id": fid,
        "plan_type": plan,
        "base_amount": float(base),
        "discount_amount": float(disc),
        "processing_fee": float(fee),
        "total_amount": float(total),
        "gateway": CFG["payment"]["gateway"],
        "period_start": d.get("period_start"),
        "period_end": d.get("period_end")
    })


# REPLACE the existing /api/visitors/<int:vid>/approve endpoint with this:
@app.route("/api/visitors/<int:vid>/approve", methods=["PUT"])
@auth_required
def approve_visitor(vid):
    v = qone("SELECT * FROM visitor_logs WHERE id=?", (vid,))
    if not v: return err("Not found", 404)
    
    # FIX: Only current resident can approve
    if g.role in ("OWNER", "TENANT"):
        resident_uid, resident_role = _get_current_flat_resident(v["flat_id"])
        if not resident_uid or g.uid != resident_uid:
            return err(
                "Only the current resident of this flat can approve visitors. "
                f"This flat is currently occupied by a {resident_role}.",
                403
            )
    
    qrun(
        "UPDATE visitor_logs SET status='APPROVED',approved_by=?,approved_at=?,entry_time=?,updated_at=? WHERE id=?",
        (g.uid, _now(), _now(), _now(), vid)
    )
    
    return ok(r2d(qone(
        "SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",
        (vid,)
    )))


# REPLACE the existing /api/visitors/<int:vid>/reject endpoint with this:
@app.route("/api/visitors/<int:vid>/reject", methods=["PUT"])
@auth_required
def reject_visitor(vid):
    v = qone("SELECT * FROM visitor_logs WHERE id=?", (vid,))
    if not v: return err("Not found", 404)
    
    # FIX: Only current resident can reject
    if g.role in ("OWNER", "TENANT"):
        resident_uid, resident_role = _get_current_flat_resident(v["flat_id"])
        if not resident_uid or g.uid != resident_uid:
            return err("Only the current resident can reject visitors", 403)
    
    qrun(
        "UPDATE visitor_logs SET status='REJECTED',approved_by=?,approved_at=?,updated_at=? WHERE id=?",
        (g.uid, _now(), _now(), vid)
    )
    
    return ok(r2d(qone(
        "SELECT v.*,f.flat_number FROM visitor_logs v LEFT JOIN flats f ON f.id=v.flat_id WHERE v.id=?",
        (vid,)
    )))
