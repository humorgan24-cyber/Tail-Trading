"""SQLite storage. One file, safe to keep on a small persistent volume."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists kv(k text primary key, v text);

create table if not exists personas(
  name text primary key, wallet text not null,
  weight real not null default 1.0, multiplier real not null default 1.0,
  bankroll_usd real, enabled integer not null default 1,
  bootstrapped integer not null default 0, added_ts integer);

-- Polymarket activity rows already processed, per persona.
create table if not exists seen(persona text, trade_key text, ts integer,
  primary key(persona, trade_key));

-- What the copied trader holds on Polymarket (token -> shares), kept current
-- from their trade stream so a partial exit can be mirrored proportionally.
create table if not exists trader_holdings(persona text, token_id text, shares real,
  primary key(persona, token_id));

-- Polymarket (condition, outcome) -> Kalshi (ticker, side).
-- status: approved | review | rejected
create table if not exists mappings(
  condition_id text, outcome text, ticker text, side text, confidence real,
  status text, source text, reason text, poly_title text, kalshi_title text,
  updated_ts integer, primary key(condition_id, outcome));

-- What *we* hold on Kalshi, attributed to the persona that caused it.
create table if not exists my_holdings(persona text, ticker text, side text,
  count real, cost real, primary key(persona, ticker, side));

create table if not exists paper_positions(ticker text, side text, count real, cost real,
  primary key(ticker, side));

create table if not exists sell_retries(
  id integer primary key autoincrement, persona text, ticker text, side text,
  count real, min_price real, expires_ts integer, source_event integer);

create table if not exists log(
  id integer primary key autoincrement, ts integer, persona text, kind text,
  status text, title text, outcome text, src_side text, src_shares real,
  src_price real, src_usdc real, ticker text, kalshi_side text,
  count real, price real, cost real, reason text);

create table if not exists equity(ts integer primary key, equity real, cash real);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    # -- generic -----------------------------------------------------------
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def get(self, k: str, default=None):
        r = self.one("select v from kv where k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set(self, k: str, v) -> None:
        self.x("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v",
               (k, json.dumps(v)))

    # -- personas ----------------------------------------------------------
    def upsert_persona(self, name, wallet, weight=1.0, multiplier=1.0, bankroll_usd=None,
                       enabled=True, overwrite=False) -> None:
        wallet = wallet.lower().strip()
        if overwrite:
            self.x("""insert into personas(name,wallet,weight,multiplier,bankroll_usd,enabled,added_ts)
                      values(?,?,?,?,?,?,?) on conflict(name) do update set wallet=excluded.wallet,
                      weight=excluded.weight, multiplier=excluded.multiplier,
                      bankroll_usd=excluded.bankroll_usd, enabled=excluded.enabled""",
                   (name, wallet, weight, multiplier, bankroll_usd, int(enabled), int(time.time())))
        else:
            self.x("""insert or ignore into personas(name,wallet,weight,multiplier,bankroll_usd,enabled,added_ts)
                      values(?,?,?,?,?,?,?)""",
                   (name, wallet, weight, multiplier, bankroll_usd, int(enabled), int(time.time())))

    def personas(self, enabled_only=False) -> list[dict]:
        sql = "select * from personas" + (" where enabled=1" if enabled_only else "") + " order by name"
        return self.q(sql)

    # -- holdings ----------------------------------------------------------
    def add_holding(self, table: str, keys: dict, d_count: float, d_cost: float) -> None:
        cols = list(keys)
        where = " and ".join(f"{c}=?" for c in cols)
        row = self.one(f"select count, cost from {table} where {where}", tuple(keys.values()))
        if row is None:
            if d_count <= 1e-9:
                return
            self.x(f"insert into {table}({','.join(cols)},count,cost) values({','.join('?'*len(cols))},?,?)",
                   (*keys.values(), d_count, d_cost))
            return
        count, cost = row["count"] + d_count, row["cost"] + d_cost
        if count <= 1e-6:
            self.x(f"delete from {table} where {where}", tuple(keys.values()))
        else:
            self.x(f"update {table} set count=?, cost=? where {where}", (count, max(cost, 0.0), *keys.values()))

    def my_holding(self, persona, ticker, side) -> dict | None:
        return self.one("select * from my_holdings where persona=? and ticker=? and side=?",
                        (persona, ticker, side))

    def trader_shares(self, persona, token_id) -> float | None:
        r = self.one("select shares from trader_holdings where persona=? and token_id=?", (persona, token_id))
        return None if r is None else r["shares"]

    def set_trader_shares(self, persona, token_id, shares) -> None:
        self.x("""insert into trader_holdings(persona,token_id,shares) values(?,?,?)
                  on conflict(persona,token_id) do update set shares=excluded.shares""",
               (persona, token_id, max(shares, 0.0)))

    # -- log ---------------------------------------------------------------
    def log(self, **f) -> int:
        f.setdefault("ts", int(time.time()))
        cols = ",".join(f)
        return self.x(f"insert into log({cols}) values({','.join('?'*len(f))})", tuple(f.values()))
