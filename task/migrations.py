"""The schema of the task store's database, as the migrations building it."""

# Applied in order; PRAGMA user_version counts how many have run. Never edit
# one that has shipped: append another.
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
)
