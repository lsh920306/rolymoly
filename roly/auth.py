"""Personal registration and credentials; password work precedes writer locks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid

from .member_profile import UNSET, validate_current_tier


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def username_value(value):
    value = str(value).strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", value):
        raise ValueError("로그인 아이디는 영문·숫자·._- 3~40자로 입력해주세요.")
    return value


def password_material(password, salt=None):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise ValueError("비밀번호는 10~256자로 입력해주세요.")
    salt = salt or secrets.token_hex(16)
    return salt, password_digest(password, salt)


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()


AUTH_DDL = """
CREATE TABLE IF NOT EXISTS registration_requests(
    request_key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
    member_id INTEGER NOT NULL UNIQUE REFERENCES members(id),
    status TEXT NOT NULL CHECK(status IN ('PENDING','REJECTED','APPROVED')),
    rejection_reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets(
    token_hash TEXT PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES accounts(id),
    expires_at TEXT NOT NULL,created_at TEXT NOT NULL,used_at TEXT,
    issued_by INTEGER NOT NULL REFERENCES accounts(id));
CREATE INDEX IF NOT EXISTS password_reset_account ON password_resets(account_id);
"""


class PersonalAuth:
    def register_member(self, username, password, riot_id, main_role, sub_role, notes="", *, request_key, current_tier="", current_tier_lp=None):
        from .core import identity, role
        username = username_value(username)
        try:
            request_uuid = uuid.UUID(str(request_key))
            if request_uuid.int == 0:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("가입 요청의 고유 UUID가 필요합니다.") from None
        request_key = str(request_uuid)
        display, canonical = identity(riot_id)
        main_role, sub_role = role(main_role), role(sub_role)
        if main_role == sub_role:
            raise ValueError("주 포지션과 부 포지션은 달라야 합니다.")
        notes = str(notes)[:2000]
        current_tier, current_tier_lp = validate_current_tier(current_tier, current_tier_lp)
        if not self.has_admin():
            raise PermissionError("관리자 설정 후 가입 신청을 시작할 수 있습니다.")
        # The unique request UUID supplies a stable public salt for safe retries.
        # Only the expensive verifier's digest enters the receipt fingerprint.
        salt, digest = password_material(password, request_uuid.hex)
        fingerprint = hashlib.sha256(json.dumps(
            [username, canonical, main_role, sub_role, notes, digest, current_tier, current_tier_lp],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with self.transaction() as db:
            if not self._active_admins(db):
                raise PermissionError("관리자 설정 후 가입 신청을 시작할 수 있습니다.")
            existing = db.execute("SELECT * FROM registration_requests WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if not hmac.compare_digest(existing["fingerprint"], fingerprint):
                    raise ValueError("같은 가입 요청 번호에 다른 내용이 전달되었습니다.")
                return {"account_id": existing["account_id"], "member_id": existing["member_id"]}
            try:
                created_at = stamp()
                member_id = db.execute("INSERT INTO members(riot_id,canonical_id,main_role,sub_role,status,base_score,application_notes,created_at,updated_at,current_tier,current_tier_lp,current_tier_updated_at) VALUES(?,?,?,?,'PENDING',0,?,?,?,?,?,?)", (display, canonical, main_role, sub_role, notes, created_at, created_at, current_tier, current_tier_lp, created_at if current_tier else None)).lastrowid
                account_id = db.execute("INSERT INTO accounts(username,display_name,password_hash,salt,role,created_at,member_id) VALUES(?,?,?,?,'member',?,?)", (username, display, digest, salt, stamp(), member_id)).lastrowid
                db.execute("INSERT INTO registration_requests(request_key,fingerprint,account_id,member_id,status,created_at,updated_at) VALUES(?,?,?,?,'PENDING',?,?)", (request_key, fingerprint, account_id, member_id, stamp(), stamp()))
            except sqlite3.IntegrityError:
                raise ValueError("이미 사용 중인 로그인 아이디 또는 Riot ID입니다. 기존 계정으로 로그인해주세요.") from None
            self._audit(db, account_id, "MEMBER_REGISTER", member_id, {"account_id": account_id, "riot_id": display, "current_tier": current_tier, "current_tier_lp": current_tier_lp})
            return {"account_id": account_id, "member_id": member_id}

    def reject_registration(self, token, member_id, reason, *, expected_updated_at=None):
        reason = str(reason).strip()
        if not reason:
            raise ValueError("가입 거절 사유를 입력해주세요.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            row = db.execute("SELECT r.status,r.updated_at AS registration_updated_at,m.status AS member_status,m.updated_at FROM registration_requests r JOIN members m ON m.id=r.member_id WHERE r.member_id=?", (member_id,)).fetchone()
            if row and expected_updated_at is not None and expected_updated_at != row["updated_at"]:
                raise ValueError("신청 내용이 변경되었습니다. 최신 신청을 불러온 뒤 다시 확인해주세요.")
            if not row or row["member_status"] != "PENDING" or row["status"] != "PENDING":
                raise ValueError("승인 대기 중인 개인 가입 신청만 거절할 수 있습니다.")
            updated_at = max(stamp(), (datetime.fromisoformat(max(row["updated_at"], row["registration_updated_at"])) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            db.execute("UPDATE registration_requests SET status='REJECTED',rejection_reason=?,updated_at=? WHERE member_id=?", (reason[:2000], updated_at, member_id))
            db.execute("UPDATE members SET updated_at=? WHERE id=?", (updated_at, member_id))
            self._audit(db, actor, "REGISTRATION_REJECT", member_id, {"reason": reason[:2000]})

    def resubmit_registration(self, token, riot_id, main_role, sub_role, notes="", *, current_tier=UNSET, current_tier_lp=UNSET, expected_updated_at=None):
        from .core import identity, role
        display, canonical = identity(riot_id)
        main_role, sub_role = role(main_role), role(sub_role)
        if main_role == sub_role:
            raise ValueError("주 포지션과 부 포지션은 달라야 합니다.")
        with self.transaction() as db:
            actor = self.require_member(db, token, approved=False)
            row = db.execute("SELECT * FROM registration_requests WHERE account_id=?", (actor["id"],)).fetchone()
            if actor["member_status"] != "PENDING" or not row or row["status"] not in ("PENDING", "REJECTED"):
                raise ValueError("승인 전 본인의 가입 신청만 수정하거나 다시 제출할 수 있습니다.")
            member = db.execute("SELECT * FROM members WHERE id=?", (actor["member_id"],)).fetchone()
            member_version = member["updated_at"]
            if expected_updated_at is not None and expected_updated_at != member_version:
                raise ValueError("신청 내용이 변경되었습니다. 최신 신청을 불러온 뒤 다시 확인해주세요.")
            current_tier, current_tier_lp = validate_current_tier(
                member["current_tier"] if current_tier is UNSET else current_tier,
                member["current_tier_lp"] if current_tier_lp is UNSET else current_tier_lp)
            updated_at = max(stamp(), (datetime.fromisoformat(max(row["updated_at"], member_version)) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            changed = (current_tier, current_tier_lp) != (member["current_tier"], member["current_tier_lp"])
            tier_updated_at = updated_at if changed else member["current_tier_updated_at"]
            try:
                db.execute("UPDATE members SET riot_id=?,canonical_id=?,main_role=?,sub_role=?,application_notes=?,current_tier=?,current_tier_lp=?,current_tier_updated_at=?,updated_at=? WHERE id=?", (display, canonical, main_role, sub_role, str(notes)[:2000], current_tier, current_tier_lp, tier_updated_at, updated_at, actor["member_id"]))
            except sqlite3.IntegrityError:
                raise ValueError("이미 신청했거나 등록된 Riot ID입니다.") from None
            db.execute("UPDATE accounts SET display_name=? WHERE id=?", (display, actor["id"]))
            db.execute("UPDATE registration_requests SET status='PENDING',rejection_reason='',updated_at=? WHERE account_id=?", (updated_at, actor["id"]))
            action = "REGISTRATION_RESUBMIT" if row["status"] == "REJECTED" else "REGISTRATION_UPDATE"
            self._audit(db, actor, action, actor["member_id"], {"riot_id": display, "application_notes_before": member["application_notes"], "application_notes": str(notes)[:2000], "current_tier_before": member["current_tier"], "current_tier_lp_before": member["current_tier_lp"], "current_tier": current_tier, "current_tier_lp": current_tier_lp})
            return actor["member_id"]

    def get_own_member(self, token):
        """Account-page projection: internal operational notes never leave it."""
        with self.read_snapshot() as db:
            actor = self.session(token, db)
            if not actor:
                raise PermissionError("로그인이 필요합니다.")
            if actor["member_id"] is None:
                return None
            member = self.get_member(actor["member_id"], db)
            member.pop("notes", None)
            return member

    def require_member(self, conn, token, approved=True):
        actor = self.session(token, conn)
        if not actor or not actor["member_id"] or actor["member_status"] not in ("PENDING", "APPROVED"):
            raise PermissionError("회원 계정 로그인이 필요합니다.")
        if approved and actor["member_status"] != "APPROVED":
            raise PermissionError("승인된 회원만 이용할 수 있습니다.")
        return actor

    def require_event_manager(self, conn, token, event_id=None):
        from .core import integer
        actor = self.session(token, conn)
        if not actor or not (actor["role"] in ("admin", "organizer") or
                             (actor["role"] == "member" and actor["member_status"] == "APPROVED")):
            raise PermissionError("승인된 회원 또는 운영 계정 로그인이 필요합니다.")
        if event_id is not None:
            event = conn.execute("SELECT created_by FROM competition_events WHERE id=?", (integer(event_id, "대회 번호"),)).fetchone()
            if not event:
                raise ValueError("내전·경매를 찾을 수 없습니다.")
            if actor["role"] != "admin" and event["created_by"] != actor["id"]:
                raise PermissionError("본인이 만든 내전·경매만 관리할 수 있습니다.")
        return actor

    def change_password(self, token, current_password, new_password):
        with self.read_snapshot() as db:
            actor = self.session(token, db)
            if not actor:
                raise PermissionError("로그인이 필요합니다.")
            account = dict(db.execute("SELECT * FROM accounts WHERE id=?", (actor["id"],)).fetchone())
        current = str(current_password)
        if len(current) > 256 or not hmac.compare_digest(password_digest(current, account["salt"]), account["password_hash"]):
            raise PermissionError("현재 비밀번호를 확인해주세요.")
        salt, digest = password_material(new_password)
        with self.transaction() as db:
            actor = self.session(token, db)
            fresh = db.execute("SELECT password_hash,salt FROM accounts WHERE id=?", (account["id"],)).fetchone()
            if not actor or not fresh or fresh["password_hash"] != account["password_hash"] or fresh["salt"] != account["salt"]:
                raise PermissionError("계정 상태가 변경되었습니다. 다시 로그인해주세요.")
            self._replace_password(db, account["id"], salt, digest)
            self._audit(db, actor, "PASSWORD_CHANGE", account["id"], {})

    def _replace_password(self, db, account_id, salt, digest):
        db.execute("UPDATE accounts SET password_hash=?,salt=? WHERE id=?", (digest, salt, account_id))
        db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
        db.execute("UPDATE password_resets SET used_at=? WHERE account_id=? AND used_at IS NULL", (stamp(), account_id))
        account = db.execute("SELECT username FROM accounts WHERE id=?", (account_id,)).fetchone()
        db.execute("DELETE FROM login_failures WHERE username=?", (account["username"],))

    def issue_password_reset(self, token, account_id, *, lifetime_minutes=30):
        from .core import integer
        lifetime_minutes = integer(lifetime_minutes, "복구 유효 시간")
        if not 1 <= lifetime_minutes <= 60:
            raise ValueError("복구 유효 시간은 1~60분이어야 합니다.")
        reset_token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            account = db.execute("SELECT a.id FROM accounts a LEFT JOIN members m ON m.id=a.member_id WHERE a.id=? AND a.active=1 AND (a.member_id IS NULL OR m.status IN ('PENDING','APPROVED'))", (integer(account_id, "계정 번호"),)).fetchone()
            if not account:
                raise ValueError("활성 계정을 찾을 수 없습니다.")
            expires = (datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes)).isoformat(timespec="microseconds")
            db.execute("UPDATE password_resets SET used_at=? WHERE account_id=? AND used_at IS NULL", (stamp(), account_id))
            db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
            db.execute("INSERT INTO password_resets(token_hash,account_id,expires_at,created_at,issued_by) VALUES(?,?,?,?,?)", (hashlib.sha256(reset_token.encode()).hexdigest(), account_id, expires, stamp(), actor["id"]))
            self._audit(db, actor, "PASSWORD_RESET_ISSUE", account_id, {"expires_at": expires})
        return {"token": reset_token, "expires_at": expires}

    def reset_password(self, reset_token, new_password):
        token_hash = hashlib.sha256(str(reset_token).encode()).hexdigest()
        with self.read_snapshot() as db:
            reset = db.execute("SELECT r.account_id FROM password_resets r JOIN accounts a ON a.id=r.account_id LEFT JOIN members m ON m.id=a.member_id WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND a.active=1 AND (a.member_id IS NULL OR m.status IN ('PENDING','APPROVED'))", (token_hash, stamp())).fetchone()
            if not reset:
                raise PermissionError("복구 코드가 만료되었거나 이미 사용되었습니다.")
        salt, digest = password_material(new_password)
        with self.transaction() as db:
            reset = db.execute("SELECT r.account_id FROM password_resets r JOIN accounts a ON a.id=r.account_id LEFT JOIN members m ON m.id=a.member_id WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND a.active=1 AND (a.member_id IS NULL OR m.status IN ('PENDING','APPROVED'))", (token_hash, stamp())).fetchone()
            if not reset:
                raise PermissionError("복구 코드가 만료되었거나 이미 사용되었습니다.")
            self._replace_password(db, reset["account_id"], salt, digest)
            self._audit(db, reset["account_id"], "PASSWORD_RESET", reset["account_id"], {})
