"""Saved clan profile, independent of match rules."""
from contextlib import closing
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from roly.core import integer, now


class Lounge:
    def __init__(self, core):
        self.core = core
        if core.is_postgres:
            return
        with core.transaction() as db:
            db.execute("""
            CREATE TABLE IF NOT EXISTS clan_profile(
                id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', founded_on TEXT NOT NULL DEFAULT '',
                capacity INTEGER, contact_url TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                poster TEXT NOT NULL DEFAULT 'rolymoly');
            """)
            if 'poster' not in {row[1] for row in db.execute('PRAGMA table_info(clan_profile)')}:
                db.execute("ALTER TABLE clan_profile ADD COLUMN poster TEXT NOT NULL DEFAULT 'rolymoly'")
            db.execute("UPDATE clan_profile SET poster='rolymoly' WHERE poster<>'rolymoly'")
            db.execute("INSERT OR IGNORE INTO clan_profile(id,name,updated_at) VALUES(1,'롤리몰리',?)", (now(),))

    def profile(self):
        with closing(self.core.connect()) as db:
            return dict(db.execute("SELECT * FROM clan_profile WHERE id=1").fetchone())

    def update_profile(self, token, *, name, description='', founded_on='', capacity=None, contact_url='', expected_updated_at=None):
        """Save a reviewed version; an already applied payload is a harmless retry."""
        name, description, contact_url = str(name).strip(), str(description).strip(), str(contact_url).strip()
        if not name or len(name) > 60 or len(description) > 2000:
            raise ValueError('클랜명은 1~60자, 소개는 2,000자 이내로 입력해 주세요.')
        if founded_on:
            founded_on = date.fromisoformat(str(founded_on)).isoformat()
        if capacity is not None:
            capacity = integer(capacity, '회원 정원')
            if capacity < 1 or capacity > 10000:
                raise ValueError('회원 정원은 1~10,000명으로 입력해 주세요.')
        if contact_url:
            url = urlsplit(contact_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or len(contact_url) > 500 or any(c.isspace() for c in contact_url):
                raise ValueError('연락 링크는 올바른 https 주소로 입력해 주세요.')
        with self.core.transaction() as db:
            actor = self.core.require_staff(db, token)
            if actor['role'] != 'admin':
                raise PermissionError('클랜 소개는 관리자만 수정할 수 있습니다.')
            before = dict(db.execute('SELECT * FROM clan_profile WHERE id=1').fetchone())
            values = {'name': name, 'description': description, 'founded_on': founded_on,
                      'capacity': capacity, 'contact_url': contact_url}
            if all(before[key] == value for key, value in values.items()):
                return before
            if expected_updated_at is not None and expected_updated_at != before['updated_at']:
                raise ValueError('클랜 소개가 변경되어 저장하지 않았습니다. 입력은 유지했습니다. 최신 정보를 불러온 뒤 다시 수정해 주세요.')
            updated_at = max(now(), (datetime.fromisoformat(before['updated_at']) + timedelta(microseconds=1)).isoformat(timespec='microseconds'))
            db.execute('UPDATE clan_profile SET name=?,description=?,founded_on=?,capacity=?,contact_url=?,updated_at=? WHERE id=1',
                       (name, description, founded_on, capacity, contact_url, updated_at))
            self.core._audit(db, actor, 'CLAN_PROFILE_UPDATE', 1, {'before': before, 'name': name, 'description': description, 'founded_on': founded_on, 'capacity': capacity, 'contact_url': contact_url})
            return dict(before, **values, updated_at=updated_at)
