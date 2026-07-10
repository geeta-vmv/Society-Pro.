-- ══════════════════════════════════════════════════════════════════════════════
-- SocietyPro Database Migrations - Owner/Tenant Fixes
-- Run this after initial setup to add new features
-- ══════════════════════════════════════════════════════════════════════════════

-- Migration 1: Add maintenance responsibility tracking to flats
ALTER TABLE flats ADD COLUMN maintenance_paid_by TEXT DEFAULT 'OWNER';
-- Values: 'OWNER' | 'TENANT' | 'SHARED'

-- Migration 2: Add maintenance payment flag to tenants
ALTER TABLE tenants ADD COLUMN pays_maintenance INTEGER DEFAULT 0;
-- 0 = owner pays, 1 = tenant pays

ALTER TABLE tenants ADD COLUMN security_deposit REAL DEFAULT 0;
ALTER TABLE tenants ADD COLUMN deposit_refunded INTEGER DEFAULT 0;
ALTER TABLE tenants ADD COLUMN deposit_refund_date TEXT;
ALTER TABLE tenants ADD COLUMN utilities_config TEXT;
-- JSON: {"electricity": "TENANT", "water": "OWNER", "gas": "TENANT"}

ALTER TABLE tenants ADD COLUMN notice_period_days INTEGER DEFAULT 30;
ALTER TABLE tenants ADD COLUMN move_out_requested INTEGER DEFAULT 0;
ALTER TABLE tenants ADD COLUMN move_out_date TEXT;

-- Migration 3: Create rent bills table
CREATE TABLE IF NOT EXISTS rent_bills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flat_id INTEGER NOT NULL,
  owner_id INTEGER NOT NULL,
  bill_month TEXT NOT NULL,
  rent_amount REAL NOT NULL,
  penalty_amount REAL DEFAULT 0,
  total_amount REAL NOT NULL,
  due_date TEXT NOT NULL,
  status TEXT DEFAULT 'PENDING',
  paid_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(flat_id) REFERENCES flats(id),
  FOREIGN KEY(owner_id) REFERENCES flat_owners(id)
);

CREATE INDEX IF NOT EXISTS ix_rent_tenant ON rent_bills(tenant_id, bill_month);
CREATE INDEX IF NOT EXISTS ix_rent_owner ON rent_bills(owner_id);
CREATE INDEX IF NOT EXISTS ix_rent_status ON rent_bills(status);

-- Migration 4: Create rent payments table
CREATE TABLE IF NOT EXISTS rent_payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_number TEXT UNIQUE NOT NULL,
  rent_bill_id INTEGER,
  tenant_id INTEGER NOT NULL,
  flat_id INTEGER NOT NULL,
  owner_id INTEGER NOT NULL,
  paid_by INTEGER NOT NULL,
  amount REAL NOT NULL,
  payment_method TEXT,
  upi_app TEXT,
  card_last4 TEXT,
  pg_order_id TEXT,
  pg_payment_id TEXT,
  status TEXT DEFAULT 'SUCCESS',
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(rent_bill_id) REFERENCES rent_bills(id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(paid_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS ix_rent_pay_tenant ON rent_payments(tenant_id);
CREATE INDEX IF NOT EXISTS ix_rent_pay_owner ON rent_payments(owner_id);

-- Migration 5: Create lease documents table
CREATE TABLE IF NOT EXISTS lease_documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flat_id INTEGER NOT NULL,
  document_type TEXT NOT NULL,
  -- Types: 'LEASE_AGREEMENT', 'ID_PROOF', 'MOVE_IN_CHECKLIST', 'MOVE_OUT_CHECKLIST', 'OTHER'
  file_name TEXT NOT NULL,
  file_path TEXT NOT NULL,
  file_size INTEGER,
  uploaded_by INTEGER,
  uploaded_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(uploaded_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS ix_doc_tenant ON lease_documents(tenant_id);

-- Migration 6: Create move-out requests table
CREATE TABLE IF NOT EXISTS move_out_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  flat_id INTEGER NOT NULL,
  owner_id INTEGER NOT NULL,
  requested_at TEXT NOT NULL,
  notice_period_days INTEGER DEFAULT 30,
  expected_exit_date TEXT NOT NULL,
  actual_exit_date TEXT,
  status TEXT DEFAULT 'PENDING',
  -- Status: PENDING, OWNER_APPROVED, INSPECTION_SCHEDULED, INSPECTION_DONE, COMPLETED, CANCELLED
  owner_approved_at TEXT,
  inspection_date TEXT,
  inspection_notes TEXT,
  final_dues REAL DEFAULT 0,
  deposit_refund_amount REAL DEFAULT 0,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id),
  FOREIGN KEY(owner_id) REFERENCES flat_owners(id)
);

CREATE INDEX IF NOT EXISTS ix_moveout_tenant ON move_out_requests(tenant_id);
CREATE INDEX IF NOT EXISTS ix_moveout_status ON move_out_requests(status);

-- Migration 7: Add owner approval for service requests
ALTER TABLE service_requests ADD COLUMN requires_owner_approval INTEGER DEFAULT 0;
ALTER TABLE service_requests ADD COLUMN owner_approved_at TEXT;
ALTER TABLE service_requests ADD COLUMN owner_approval_note TEXT;
ALTER TABLE service_requests ADD COLUMN estimated_cost REAL DEFAULT 0;
ALTER TABLE service_requests ADD COLUMN actual_cost REAL DEFAULT 0;
ALTER TABLE service_requests ADD COLUMN cost_borne_by TEXT DEFAULT 'SOCIETY';
-- Values: 'SOCIETY', 'OWNER', 'TENANT', 'SHARED'

-- Migration 8: Add maintenance bill splitting
ALTER TABLE maintenance_bills ADD COLUMN split_config TEXT;
-- JSON: {"OWNER": 50, "TENANT": 50} or null for single payer
ALTER TABLE maintenance_bills ADD COLUMN split_paid_by TEXT;
-- JSON: {"OWNER": true, "TENANT": false} - tracks who paid their share

-- Migration 9: Create system settings table for automated tasks
CREATE TABLE IF NOT EXISTS system_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT UNIQUE NOT NULL,
  last_run_at TEXT,
  next_run_at TEXT,
  status TEXT DEFAULT 'IDLE',
  -- Status: IDLE, RUNNING, COMPLETED, FAILED
  run_count INTEGER DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO system_jobs(job_name, last_run_at, next_run_at, created_at, updated_at)
VALUES
  ('check_expired_leases', NULL, datetime('now'), datetime('now'), datetime('now')),
  ('generate_rent_bills', NULL, datetime('now'), datetime('now'), datetime('now')),
  ('apply_rent_penalties', NULL, datetime('now'), datetime('now'), datetime('now')),
  ('send_lease_expiry_alerts', NULL, datetime('now'), datetime('now'), datetime('now'));

-- Migration 10: Add notification preferences
CREATE TABLE IF NOT EXISTS notification_preferences(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER UNIQUE NOT NULL,
  email_enabled INTEGER DEFAULT 1,
  sms_enabled INTEGER DEFAULT 1,
  push_enabled INTEGER DEFAULT 1,
  notify_visitor_approval INTEGER DEFAULT 1,
  notify_maintenance_due INTEGER DEFAULT 1,
  notify_rent_due INTEGER DEFAULT 1,
  notify_lease_expiry INTEGER DEFAULT 1,
  notify_service_updates INTEGER DEFAULT 1,
  notify_announcements INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

-- Migration 11: Add audit log for important actions
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  -- Actions: 'TENANT_ADDED', 'TENANT_REMOVED', 'LEASE_EXPIRED', 'RENT_PAID', 'MAINTENANCE_PAID', etc.
  entity_type TEXT,
  -- Types: 'TENANT', 'FLAT', 'PAYMENT', 'BILL', etc.
  entity_id INTEGER,
  old_value TEXT,
  new_value TEXT,
  ip_address TEXT,
  user_agent TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS ix_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_logs(entity_type, entity_id);

-- ══════════════════════════════════════════════════════════════════════════════
-- Data Cleanup and Validation
-- ══════════════════════════════════════════════════════════════════════════════

-- Set default maintenance_paid_by for existing flats
UPDATE flats 
SET maintenance_paid_by = CASE 
  WHEN status = 'TENANT_OCCUPIED' THEN 'TENANT'
  ELSE 'OWNER'
END
WHERE maintenance_paid_by IS NULL;

-- Update tenants to pay maintenance if they occupy the flat
UPDATE tenants 
SET pays_maintenance = 1 
WHERE is_active = 1 
  AND lease_end >= date('now')
  AND flat_id IN (SELECT id FROM flats WHERE status = 'TENANT_OCCUPIED');

-- ══════════════════════════════════════════════════════════════════════════════
-- Views for easier queries
-- ══════════════════════════════════════════════════════════════════════════════

-- View: Active tenants with flat and owner details
CREATE VIEW IF NOT EXISTS v_active_tenants AS
SELECT 
  t.id as tenant_id,
  t.user_id,
  u.name as tenant_name,
  u.mobile as tenant_mobile,
  u.email as tenant_email,
  t.flat_id,
  f.flat_number,
  f.society_id,
  s.name as society_name,
  t.owner_id,
  o.name as owner_name,
  o.mobile as owner_mobile,
  t.lease_start,
  t.lease_end,
  t.monthly_rent,
  t.security_deposit,
  t.pays_maintenance,
  CAST((julianday(t.lease_end) - julianday('now')) AS INTEGER) as days_until_expiry,
  CASE 
    WHEN t.lease_end < date('now') THEN 'EXPIRED'
    WHEN julianday(t.lease_end) - julianday('now') <= 30 THEN 'EXPIRING_SOON'
    ELSE 'ACTIVE'
  END as lease_status
FROM tenants t
JOIN users u ON u.id = t.user_id
JOIN flats f ON f.id = t.flat_id
JOIN societies s ON s.id = f.society_id
JOIN flat_owners fo ON fo.id = t.owner_id
JOIN users o ON o.id = fo.user_id
WHERE t.is_active = 1;

-- View: Pending rent bills
CREATE VIEW IF NOT EXISTS v_pending_rent_bills AS
SELECT 
  rb.*,
  t.user_id as tenant_user_id,
  u.name as tenant_name,
  u.mobile as tenant_mobile,
  f.flat_number,
  fo.user_id as owner_user_id,
  o.name as owner_name,
  CAST((julianday('now') - julianday(rb.due_date)) AS INTEGER) as days_overdue
FROM rent_bills rb
JOIN tenants t ON t.id = rb.tenant_id
JOIN users u ON u.id = t.user_id
JOIN flats f ON f.id = rb.flat_id
JOIN flat_owners fo ON fo.id = rb.owner_id
JOIN users o ON o.id = fo.user_id
WHERE rb.status = 'PENDING';

-- ══════════════════════════════════════════════════════════════════════════════
-- Migration Complete
-- ══════════════════════════════════════════════════════════════════════════════

-- To apply migrations:
-- sqlite3 societypro.db < migrations.sql
