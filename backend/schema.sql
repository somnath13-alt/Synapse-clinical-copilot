BEGIN IMMEDIATE;

CREATE TABLE foundation_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    baseline TEXT NOT NULL
);

INSERT INTO foundation_metadata (singleton, schema_version, baseline)
VALUES (1, 1, 'foundation-empty-v1');

PRAGMA user_version = 1;

COMMIT;

