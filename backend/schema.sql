BEGIN IMMEDIATE;

CREATE TABLE foundation_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    baseline TEXT NOT NULL
);

INSERT INTO foundation_metadata (singleton, schema_version, baseline)
VALUES (1, 3, 'foundation-empty-v3');

PRAGMA user_version = 3;

COMMIT;
