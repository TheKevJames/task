"""The schema of the task store's database, as the migrations building it."""

# Applied in order; PRAGMA user_version counts how many have run. Never edit
# one that has shipped: append another.
#
# They run with foreign keys off, so that rebuilding a table (create, copy,
# drop, rename) neither cascades into its children nor is refused by them;
# db._migrate checks that every foreign key still holds before committing.
MIGRATIONS = (
    # `seq` is the highest id the namespace has handed out, so that ids are
    # never reused, even once the task holding the highest is gone.
    """
    CREATE TABLE namespace (
        name TEXT PRIMARY KEY
            CHECK (name GLOB '[a-z0-9]*' AND name NOT GLOB '*[^a-z0-9_.-]*'),
        seq INTEGER NOT NULL DEFAULT 0 CHECK (seq >= 0)
    ) STRICT, WITHOUT ROWID
    """,
    "INSERT INTO namespace (name) VALUES ('default')",
    """
    CREATE TABLE task (
        namespace TEXT NOT NULL REFERENCES namespace (name),
        id INTEGER NOT NULL CHECK (id > 0),
        summary TEXT NOT NULL
            CHECK (trim(summary) != '' AND instr(summary, char(10)) = 0),
        tag TEXT NOT NULL
            CHECK (tag GLOB '[a-z0-9]*' AND tag NOT GLOB '*[^a-z0-9_.-]*'),
        next TEXT CHECK (date(next) IS next),
        interval TEXT CHECK (
            interval GLOB '[1-9]*[dwm]'
            AND substr(interval, 1, length(interval) - 1) NOT GLOB '*[^0-9]*'
        ),
        shift INTEGER NOT NULL DEFAULT 0 CHECK (shift IN (0, 1)),
        claim TEXT CHECK (
            claim != '' AND claim NOT GLOB '*[^A-Za-z0-9_./-]*'
        ),
        link TEXT CHECK (
            link != '' AND link NOT GLOB ('*[ ' || char(9, 10, 13) || ']*')
        ),
        priority TEXT CHECK (priority IN ('low', 'medium', 'high')),
        size TEXT CHECK (size IN ('small', 'medium', 'large')),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        updated TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated) IS updated),
        PRIMARY KEY (namespace, id),
        CHECK (next IS NOT NULL OR (interval IS NULL AND shift = 0)),
        CHECK (updated >= created)
    ) STRICT
    """,
    """
    CREATE TRIGGER task_seq AFTER INSERT ON task BEGIN
        UPDATE namespace SET seq = max(seq, NEW.id)
        WHERE name = NEW.namespace;
    END
    """,
    # Moving a task changes its key in place, so that its conversation follows
    # it by ON UPDATE CASCADE.
    """
    CREATE TRIGGER task_seq_move AFTER UPDATE OF namespace, id ON task BEGIN
        UPDATE namespace SET seq = max(seq, NEW.id)
        WHERE name = NEW.namespace;
    END
    """,
    # A task's conversation: who said what, and when, as the store stamped it.
    # Answers are rows of their own, so that no row is ever updated.
    """
    CREATE TABLE entry (
        id INTEGER PRIMARY KEY,
        namespace TEXT NOT NULL,
        task INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('comment', 'question', 'answer')),
        question INTEGER UNIQUE REFERENCES entry (id),
        actor TEXT NOT NULL CHECK (
            actor != '' AND actor NOT GLOB '*[^A-Za-z0-9_./-]*'
        ),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        body TEXT NOT NULL CHECK (
            body != '' AND trim(body, ' ' || char(9, 10, 13)) = body
        ),
        FOREIGN KEY (namespace, task) REFERENCES task (namespace, id)
            ON DELETE CASCADE ON UPDATE CASCADE,
        CHECK ((kind = 'answer') = (question IS NOT NULL)),
        CHECK (kind != 'answer' OR actor = 'operator')
    ) STRICT
    """,
    'CREATE INDEX entry_task ON entry (namespace, task)',
    """
    CREATE TRIGGER entry_answers_a_question BEFORE INSERT ON entry
    WHEN NEW.kind = 'answer' BEGIN
        SELECT RAISE(ABORT, 'an answer must answer a question on its task')
        WHERE NOT EXISTS (
            SELECT 1 FROM entry
            WHERE id = NEW.question AND kind = 'question'
                AND namespace = NEW.namespace AND task = NEW.task
        );
    END
    """,
    # A task's relations: what it waits on, as `relations` describes. Only
    # uncleared ones are kept, so a task with any is blocked. Deleting a task
    # which another waits on fails: the store clears what waits on it first.
    """
    CREATE TABLE blocker (
        id INTEGER PRIMARY KEY,
        namespace TEXT NOT NULL,
        task INTEGER NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('done', 'decided', 'duplicate', 'external')),
        target_namespace TEXT,
        target INTEGER,
        url TEXT CHECK (
            url GLOB '[a-z]*://?*'
            AND url NOT GLOB ('*[ ' || char(9, 10, 13) || ']*')
        ),
        condition TEXT CHECK (
            condition != ''
            AND trim(condition, ' ' || char(9, 13)) = condition
            AND instr(condition, char(10)) = 0
        ),
        actor TEXT NOT NULL CHECK (
            actor != '' AND actor NOT GLOB '*[^A-Za-z0-9_./-]*'
        ),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        FOREIGN KEY (namespace, task) REFERENCES task (namespace, id)
            ON DELETE CASCADE ON UPDATE CASCADE,
        FOREIGN KEY (target_namespace, target) REFERENCES task (namespace, id)
            ON UPDATE CASCADE,
        CHECK (
            (kind = 'external')
            = (url IS NOT NULL AND condition IS NOT NULL)
        ),
        CHECK (
            (kind = 'external')
            = (target_namespace IS NULL AND target IS NULL)
        ),
        CHECK (target_namespace IS NOT namespace OR target IS NOT task),
        UNIQUE (namespace, task, target_namespace, target),
        UNIQUE (namespace, task, url)
    ) STRICT
    """,
    'CREATE INDEX blocker_target ON blocker (target_namespace, target)',
    """
    CREATE UNIQUE INDEX blocker_one_canonical ON blocker (namespace, task)
    WHERE kind = 'duplicate'
    """,
    # Actors' names are lowercase. Dropping a table drops its indexes and
    # triggers, so each rebuild makes them again.
    """
    CREATE TABLE task_new (
        namespace TEXT NOT NULL REFERENCES namespace (name),
        id INTEGER NOT NULL CHECK (id > 0),
        summary TEXT NOT NULL
            CHECK (trim(summary) != '' AND instr(summary, char(10)) = 0),
        tag TEXT NOT NULL
            CHECK (tag GLOB '[a-z0-9]*' AND tag NOT GLOB '*[^a-z0-9_.-]*'),
        next TEXT CHECK (date(next) IS next),
        interval TEXT CHECK (
            interval GLOB '[1-9]*[dwm]'
            AND substr(interval, 1, length(interval) - 1) NOT GLOB '*[^0-9]*'
        ),
        shift INTEGER NOT NULL DEFAULT 0 CHECK (shift IN (0, 1)),
        claim TEXT CHECK (
            claim != '' AND claim NOT GLOB '*[^a-z0-9_./-]*'
        ),
        link TEXT CHECK (
            link != '' AND link NOT GLOB ('*[ ' || char(9, 10, 13) || ']*')
        ),
        priority TEXT CHECK (priority IN ('low', 'medium', 'high')),
        size TEXT CHECK (size IN ('small', 'medium', 'large')),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        updated TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated) IS updated),
        PRIMARY KEY (namespace, id),
        CHECK (next IS NOT NULL OR (interval IS NULL AND shift = 0)),
        CHECK (updated >= created)
    ) STRICT
    """,
    """
    INSERT INTO task_new
    SELECT namespace, id, summary, tag, next, interval, shift, lower(claim),
        link, priority, size, created, updated
    FROM task
    """,
    'DROP TABLE task',
    'ALTER TABLE task_new RENAME TO task',
    """
    CREATE TRIGGER task_seq AFTER INSERT ON task BEGIN
        UPDATE namespace SET seq = max(seq, NEW.id)
        WHERE name = NEW.namespace;
    END
    """,
    """
    CREATE TRIGGER task_seq_move AFTER UPDATE OF namespace, id ON task BEGIN
        UPDATE namespace SET seq = max(seq, NEW.id)
        WHERE name = NEW.namespace;
    END
    """,
    """
    CREATE TABLE entry_new (
        id INTEGER PRIMARY KEY,
        namespace TEXT NOT NULL,
        task INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('comment', 'question', 'answer')),
        question INTEGER UNIQUE REFERENCES entry (id),
        actor TEXT NOT NULL CHECK (
            actor != '' AND actor NOT GLOB '*[^a-z0-9_./-]*'
        ),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        body TEXT NOT NULL CHECK (
            body != '' AND trim(body, ' ' || char(9, 10, 13)) = body
        ),
        FOREIGN KEY (namespace, task) REFERENCES task (namespace, id)
            ON DELETE CASCADE ON UPDATE CASCADE,
        CHECK ((kind = 'answer') = (question IS NOT NULL)),
        CHECK (kind != 'answer' OR actor = 'operator')
    ) STRICT
    """,
    """
    INSERT INTO entry_new
    SELECT id, namespace, task, kind, question, lower(actor), created, body
    FROM entry
    """,
    'DROP TABLE entry',
    'ALTER TABLE entry_new RENAME TO entry',
    'CREATE INDEX entry_task ON entry (namespace, task)',
    """
    CREATE TRIGGER entry_answers_a_question BEFORE INSERT ON entry
    WHEN NEW.kind = 'answer' BEGIN
        SELECT RAISE(ABORT, 'an answer must answer a question on its task')
        WHERE NOT EXISTS (
            SELECT 1 FROM entry
            WHERE id = NEW.question AND kind = 'question'
                AND namespace = NEW.namespace AND task = NEW.task
        );
    END
    """,
    """
    CREATE TABLE blocker_new (
        id INTEGER PRIMARY KEY,
        namespace TEXT NOT NULL,
        task INTEGER NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('done', 'decided', 'duplicate', 'external')),
        target_namespace TEXT,
        target INTEGER,
        url TEXT CHECK (
            url GLOB '[a-z]*://?*'
            AND url NOT GLOB ('*[ ' || char(9, 10, 13) || ']*')
        ),
        condition TEXT CHECK (
            condition != ''
            AND trim(condition, ' ' || char(9, 13)) = condition
            AND instr(condition, char(10)) = 0
        ),
        actor TEXT NOT NULL CHECK (
            actor != '' AND actor NOT GLOB '*[^a-z0-9_./-]*'
        ),
        created TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created) IS created),
        FOREIGN KEY (namespace, task) REFERENCES task (namespace, id)
            ON DELETE CASCADE ON UPDATE CASCADE,
        FOREIGN KEY (target_namespace, target) REFERENCES task (namespace, id)
            ON UPDATE CASCADE,
        CHECK (
            (kind = 'external')
            = (url IS NOT NULL AND condition IS NOT NULL)
        ),
        CHECK (
            (kind = 'external')
            = (target_namespace IS NULL AND target IS NULL)
        ),
        CHECK (target_namespace IS NOT namespace OR target IS NOT task),
        UNIQUE (namespace, task, target_namespace, target),
        UNIQUE (namespace, task, url)
    ) STRICT
    """,
    """
    INSERT INTO blocker_new
    SELECT id, namespace, task, kind, target_namespace, target, url,
        condition, lower(actor), created
    FROM blocker
    """,
    'DROP TABLE blocker',
    'ALTER TABLE blocker_new RENAME TO blocker',
    'CREATE INDEX blocker_target ON blocker (target_namespace, target)',
    """
    CREATE UNIQUE INDEX blocker_one_canonical ON blocker (namespace, task)
    WHERE kind = 'duplicate'
    """,
)
