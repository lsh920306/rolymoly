"""Saved clan profile, independent of match rules."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
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

    def home_summary(self, current_time):
        """Read only the public event/history fields displayed on the lounge.

        Core writes played_at as fixed-width UTC ISO text. UTC bounds therefore
        preserve the Korean calendar month without downloading every game or
        parsing historical archive payloads that will never be displayed.
        """
        if current_time.tzinfo is None:
            raise ValueError("시간대가 포함된 기준 시각이 필요합니다.")
        korean = current_time.astimezone(timezone(timedelta(hours=9)))
        start = korean.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
        bounds = tuple(stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
                       for stamp in (start, end))
        with self.core.read_snapshot() as db:
            active = [dict(row) for row in db.execute("""SELECT
                e.id,e.title,e.kind,e.team_count,e.format,e.status,e.starts_at,e.created_at,
                (SELECT COUNT(*) FROM competition_players p
                 WHERE p.event_id=e.id AND p.participation_status='SELECTED') AS participant_count
                FROM competition_events e WHERE e.status NOT IN ('COMPLETED','CANCELLED')
                ORDER BY e.id DESC""")]
            recent = [dict(row) for row in db.execute("""SELECT id,kind,played_at,winner
                FROM games WHERE status='CONFIRMED' ORDER BY played_at DESC,id DESC LIMIT 5""")]
            count = db.execute("""SELECT COUNT(*) FROM games
                WHERE status='CONFIRMED' AND played_at>=? AND played_at<?""", bounds).fetchone()[0]
            labels = {}
            if recent:
                from .competition import _table_exists
                ids = tuple(game['id'] for game in recent)
                marks = ','.join('?' for _ in ids)
                if _table_exists(db, 'competition_game_archives'):
                    for row in db.execute(f"""SELECT a.core_game_id,a.event_id,a.snapshot,e.title
                        FROM competition_game_archives a JOIN competition_events e ON e.id=a.event_id
                        WHERE a.core_game_id IN ({marks}) ORDER BY a.id""", ids):
                        saved = json.loads(row['snapshot'])
                        labels[row['core_game_id']] = {
                            'core_game_id': row['core_game_id'], 'event_id': row['event_id'],
                            'title': row['title'], 'team_a_name': saved['team_a_name'],
                            'team_b_name': saved['team_b_name'], 'winner_name': saved['winner_name']}
                rows = db.execute(f"""SELECT g.core_game_id,g.event_id,e.title,
                    a.name AS team_a_name,b.name AS team_b_name,w.name AS winner_name
                    FROM competition_games g JOIN competition_events e ON e.id=g.event_id
                    LEFT JOIN competition_teams a ON a.id=g.team_a
                    LEFT JOIN competition_teams b ON b.id=g.team_b
                    LEFT JOIN competition_teams w ON w.id=g.winner_team_id
                    WHERE g.core_game_id IN ({marks})""", ids)
                labels.update({row['core_game_id']: dict(row) for row in rows})
            return {'active_events': active, 'recent_games': recent,
                    'month_game_count': count, 'game_labels': labels}

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
