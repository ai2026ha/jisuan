#!/usr/bin/env python3
"""把网页“导出当前数据”生成的 ZIP 导入自建 PostgreSQL。

默认拒绝覆盖非空数据库。只有显式传 --replace 才会清空六张业务表后导入。
先启动应用一次，让 SQLAlchemy 创建表结构，再执行本脚本。
"""
import argparse
import json
import os
import sys
import zipfile
from pathlib import Path

import psycopg2
from psycopg2 import sql

TABLES = ["users", "agents", "games", "rates", "calculations", "manual_adjustments"]


def normalize_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


def load_backup(path: Path):
    with zipfile.ZipFile(path, "r") as zf:
        payload = json.loads(zf.read("all_data.json").decode("utf-8"))
    data = payload.get("data") or {}
    for table in TABLES:
        if table not in data or not isinstance(data[table], list):
            raise ValueError(f"备份缺少数据表: {table}")
    return payload


def main():
    parser = argparse.ArgumentParser(description="导入当前有效数据备份到 PostgreSQL")
    parser.add_argument("backup", help="网页导出的 当前数据_*.zip")
    parser.add_argument("--replace", action="store_true", help="允许清空目标六张表后导入")
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL", "").strip()

    payload = load_backup(Path(args.backup))
    data = payload["data"]

    if dsn:
        conn = psycopg2.connect(normalize_dsn(dsn))
    elif os.getenv("DB_HOST", "").strip():
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "db"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "agent_calculator")),
            user=os.getenv("DB_USER", os.getenv("POSTGRES_USER", "agent_calc")),
            password=os.getenv("DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
        )
    else:
        print("错误：请设置 DATABASE_URL，或提供 DB_HOST/DB_NAME/DB_USER/DB_PASSWORD。", file=sys.stderr)
        return 2
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            user_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM agents")
            agent_count = cur.fetchone()[0]
            if (user_count or agent_count) and not args.replace:
                raise RuntimeError("目标数据库不是空库；如确定覆盖，请重新执行并增加 --replace")

            if args.replace:
                # FK 依赖表优先清空；RESTART IDENTITY 重置序列。
                cur.execute(
                    "TRUNCATE TABLE manual_adjustments, calculations, rates, games, agents, users RESTART IDENTITY CASCADE"
                )

            for table in TABLES:
                rows = data[table]
                if not rows:
                    continue
                columns = list(rows[0].keys())
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                for row in rows:
                    cur.execute(query, [row.get(c) for c in columns])

            for table in TABLES:
                cur.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM {}), 1), "
                        "COALESCE((SELECT MAX(id) FROM {}), 0) > 0)"
                    ).format(sql.Identifier(table), sql.Identifier(table)),
                    [table],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("导入完成：users / agents / games / rates / calculations / manual_adjustments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
