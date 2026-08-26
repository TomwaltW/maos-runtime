"""重新锁定契约指纹。仅在手册明确授权的增量变更后运行，
运行后必须在 docs/DECISIONS.md 记录一行理由。"""
import hashlib, json, os, pathlib, sqlite3, tempfile, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FROZEN_FILES = [
    "maos/contracts/events.py",
    "maos/contracts/states.py",
]

def _norm(sql: str) -> str:
    return hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()

def snapshot_tables() -> dict:
    from maos.core.store import SqliteStore
    with tempfile.TemporaryDirectory() as d:
        db = pathlib.Path(d) / "probe.db"
        SqliteStore(str(db)).init_schema()    # 触发建表
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "select name, sql from sqlite_master "
            "where type='table' and name not like 'sqlite_%'"
        ).fetchall()
        conn.close()
    return {name: _norm(sql) for name, sql in rows if sql}

def main():
    if os.environ.get("MAOS_RELOCK") != "1":
        print("relock 未授权。", file=sys.stderr)
        sys.exit(1)
    files = {
        rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        for rel in FROZEN_FILES
    }
    payload = {"files": files, "tables": snapshot_tables()}
    (ROOT / ".contracts.lock").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"locked {len(files)} files, {len(payload['tables'])} tables")

if __name__ == "__main__":
    main()
