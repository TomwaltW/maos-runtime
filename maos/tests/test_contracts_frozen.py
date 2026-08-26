"""铁律 1 的机器强制。契约文件或既有表结构一旦变动，本测试立刻红。"""
import hashlib, json, pathlib, sqlite3, tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOCK = ROOT / ".contracts.lock"
FROZEN_FILES = [
    "maos/contracts/events.py",
    "maos/contracts/states.py",
]

def _load_lock() -> dict:
    assert LOCK.exists(), ".contracts.lock 缺失 —— Phase 0 未正确初始化"
    return json.loads(LOCK.read_text(encoding="utf-8"))

def test_frozen_contract_files_unchanged():
    lock = _load_lock()
    for rel in FROZEN_FILES:
        path = ROOT / rel
        assert path.exists(), f"{rel} 不存在"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == lock["files"][rel], (
            f"\n【铁律 1 违规】{rel} 被修改了。\n"
            f"  锁定 sha256 = {lock['files'][rel][:16]}...\n"
            f"  当前 sha256 = {actual[:16]}...\n"
            f"契约文件禁改。停止当前工作，向人类报告，不要尝试任何补救。"
        )

def test_existing_store_tables_ddl_unchanged():
    """只校验 Phase 0 时已存在的表；新增表（如 Phase 4 的 knowledge）不受影响。"""
    from maos.core.store import SqliteStore
    lock = _load_lock()
    with tempfile.TemporaryDirectory() as d:
        db = pathlib.Path(d) / "probe.db"
        SqliteStore(str(db)).init_schema()
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "select name, sql from sqlite_master "
            "where type='table' and name not like 'sqlite_%'"
        ).fetchall()
        conn.close()
    actual = {
        name: hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()
        for name, sql in rows if sql
    }
    for name, expected in lock["tables"].items():
        assert name in actual, f"【铁律 1 违规】既有表 {name} 消失了。停止并向人类报告。"
        assert actual[name] == expected, (
            f"\n【铁律 1 违规】表 {name} 的结构被改动。只允许新增表，不许改既有表。"
            f"停止当前工作，向人类报告，不要尝试任何补救。"
        )
