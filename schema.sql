CREATE TABLE IF NOT EXISTS works (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    cover_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benefits (
    id          TEXT PRIMARY KEY,
    work_id     TEXT NOT NULL REFERENCES works(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS benefit_files (
    id         TEXT PRIMARY KEY,
    benefit_id TEXT NOT NULL REFERENCES benefits(id),
    position   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    file_path  TEXT NOT NULL,  -- 特典ファイル用ディレクトリからの相対パス
    mime_type  TEXT NOT NULL,
    UNIQUE (benefit_id, position)
);

CREATE TABLE IF NOT EXISTS accounts (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entitlements (
    token      TEXT PRIMARY KEY,
    benefit_id TEXT NOT NULL REFERENCES benefits(id),
    uid        TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN (
                   'issuing', 'unused', 'account', 'download', 'revoked')),
    account_id TEXT REFERENCES accounts(id),
    issued_at  TEXT NOT NULL,
    claimed_at TEXT,
    -- アカウントに追加済みなら受取先が必要。無効にした後も受取先は残す
    CHECK (status <> 'account' OR account_id IS NOT NULL),
    CHECK (status IN ('account', 'revoked') OR account_id IS NULL)
);

-- 同じUIDで有効な取得権は1件まで。発行中は対象外
CREATE UNIQUE INDEX IF NOT EXISTS entitlements_active_uid
    ON entitlements(uid) WHERE status IN ('unused', 'account', 'download');

CREATE TABLE IF NOT EXISTS download_sessions (
    id         TEXT PRIMARY KEY,  -- Cookieに保存するランダムな値
    token      TEXT NOT NULL REFERENCES entitlements(token),
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS download_sessions_token ON download_sessions(token);

-- ローカルCLIで例外登録した物理カード。同じUIDの再発行後も適用する
CREATE TABLE IF NOT EXISTS repeatable_cards (
    uid TEXT PRIMARY KEY
);

-- 例外カードからは複数のアカウントが受け取れるため、受取先を別に保存する
CREATE TABLE IF NOT EXISTS repeatable_account_claims (
    token      TEXT NOT NULL REFERENCES entitlements(token),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (token, account_id)
);

CREATE INDEX IF NOT EXISTS repeatable_account_claims_account
    ON repeatable_account_claims(account_id);
