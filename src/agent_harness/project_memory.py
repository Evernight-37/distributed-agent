"""Shared, attributed project notes. Private conversations are never imported."""
import time


class ProjectMemory:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS project_memory_notes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    source_task_id TEXT
                );
                CREATE INDEX IF NOT EXISTS project_memory_project
                    ON project_memory_notes(project_id,id);
            ''')

    def snapshot(self, user, project):
        with self.store.connect() as db:
            self.store.require_member(db, project, user)
            rows = db.execute('''SELECT id,author_id,body,created_at,source_task_id
                                 FROM project_memory_notes WHERE project_id=?
                                 ORDER BY id DESC LIMIT 100''', (project,)).fetchall()
            return {'notes': [dict(row) for row in reversed(rows)]}

    def add_note(self, user, project, body):
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000:
            raise ValueError('项目记忆需为 1–2000 字符')
        with self.store.connect() as db:
            self.store.require_member(db, project, user)
            cursor = db.execute('''INSERT INTO project_memory_notes
                (project_id,author_id,body,created_at) VALUES(?,?,?,?)''',
                (project, user, body.strip(), time.time()))
            return {'id': cursor.lastrowid}

    def delete_note(self, user, project, note_id):
        with self.store.connect() as db:
            membership = self.store.require_member(db, project, user)
            row = db.execute('SELECT author_id FROM project_memory_notes WHERE project_id=? AND id=?',
                             (project, note_id)).fetchone()
            if not row:
                raise ValueError('记忆条目不存在')
            if user not in (row['author_id'], membership['owner_id']):
                raise PermissionError('只有作者或群主可以删除记忆')
            db.execute('DELETE FROM project_memory_notes WHERE project_id=? AND id=?',
                       (project, note_id))
            return {'deleted': True}

    def agent_context(self, project, *, limit=12000):
        """A bounded project-wide view for every agent turn, regardless of chat."""
        with self.store.connect() as db:
            rows = db.execute('''SELECT id,author_id,body,created_at
                                 FROM project_memory_notes WHERE project_id=?
                                 ORDER BY id DESC LIMIT 100''', (project,)).fetchall()
        selected = []
        size = 0
        for row in rows:
            item = dict(row)
            cost = len(item['body'])
            if size + cost > limit:
                break
            selected.append(item)
            size += cost
        return list(reversed(selected))

    def add_agent_notes(self, db, agent, project, task_id, notes):
        """Accept explicit notes only from a non-private model turn."""
        if not isinstance(notes, list) or len(notes) > 3:
            raise ValueError('项目记忆条目过多')
        for note in notes:
            if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
                raise ValueError('项目记忆条目无效')
            db.execute('''INSERT INTO project_memory_notes
                (project_id,author_id,body,created_at,source_task_id) VALUES(?,?,?,?,?)''',
                (project, agent, note.strip(), time.time(), task_id))
