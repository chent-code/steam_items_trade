CREATE TABLE IF NOT EXISTS snapshots (
  ts         TEXT NOT NULL,
  source     TEXT NOT NULL,
  item       TEXT NOT NULL,
  platform   TEXT NOT NULL,
  ask        REAL,
  ask_vol    REAL,
  bid        REAL,
  bid_vol    REAL,
  vol24      REAL,
  last_price REAL,
  raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_item ON snapshots(item, platform, ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts   ON snapshots(ts);

CREATE TABLE IF NOT EXISTS steam_sales (
  item  TEXT NOT NULL,
  date  TEXT NOT NULL,
  price REAL,
  qty   REAL
);
CREATE INDEX IF NOT EXISTS idx_sales ON steam_sales(item, date);
