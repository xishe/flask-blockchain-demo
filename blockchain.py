import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from time import time

DEFAULT_DIFFICULTY = 2
MAX_NONCE_SEARCH = 100_000
MAX_MINING_BATCH = 32
MAX_TEXT_LENGTH = 5000
GENESIS_PREV_HASH = "0" * 64
ROOT = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE blocks (
    id INTEGER PRIMARY KEY,
    index_num INTEGER NOT NULL UNIQUE,
    timestamp REAL NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    nonce INTEGER,
    is_mined INTEGER NOT NULL DEFAULT 0,
    mined_at REAL
)
"""
INSERT_BLOCK = """
INSERT INTO blocks (index_num, timestamp, data, prev_hash, content_hash, nonce, is_mined, mined_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


class LegacyChainError(ValueError):
    """The original JSON files do not form a readable chain."""


class CorruptChainError(RuntimeError):
    """The stored chain cannot safely accept writes."""


class MiningLimitError(RuntimeError):
    """The demo exhausted its proof-of-work budget."""


def get_default_db_path() -> Path:
    return Path(os.environ.get("BLOCKCHAIN_DB_PATH") or ROOT / "instance" / "blockchain.db")


@contextmanager
def _connection(db_path):
    path = Path(db_path) if db_path is not None else get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


@contextmanager
def _transaction(connection):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        yield


def compute_content_hash(index: int, timestamp: float, data: str, prev_hash: str) -> str:
    payload = json.dumps(
        {"index": index, "timestamp": float(timestamp), "data": data, "prev_hash": prev_hash},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_proof(content_hash: str, nonce: int | None, difficulty: int = DEFAULT_DIFFICULTY) -> bool:
    return (
        type(nonce) is int
        and 0 <= nonce < MAX_NONCE_SEARCH
        and type(difficulty) is int
        and 1 <= difficulty <= 64
        and hashlib.sha256(f"{content_hash}:{nonce}".encode()).hexdigest().startswith("0" * difficulty)
    )


def mine_hash(content_hash: str, difficulty: int = DEFAULT_DIFFICULTY,
              max_iterations: int = MAX_NONCE_SEARCH) -> int | None:
    for nonce in range(min(max_iterations, MAX_NONCE_SEARCH)):
        if is_valid_proof(content_hash, nonce, difficulty):
            return nonce
    return None


def _require_proof(content_hash):
    nonce = mine_hash(content_hash)
    if nonce is None:
        raise MiningLimitError("Не удалось найти доказательство в пределах лимита.")
    return nonce


def _new_block(index, timestamp, text, prev_hash, with_proof=False):
    digest = compute_content_hash(index, timestamp, text, prev_hash)
    nonce = _require_proof(digest) if with_proof else None
    return {
        "index": index,
        "timestamp": timestamp,
        "text": text,
        "prev_hash": prev_hash,
        "content_hash": digest,
        "nonce": nonce,
        "is_mined": int(nonce is not None),
        "mined_at": time() if nonce is not None else None,
    }


def _insert(connection, block):
    connection.execute(INSERT_BLOCK, tuple(block[key] for key in (
        "index", "timestamp", "text", "prev_hash", "content_hash", "nonce", "is_mined", "mined_at",
    )))


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _read_legacy(directory):
    directory = Path(directory)
    if not directory.exists():
        return []

    candidates = sorted(
        (int(path.stem), path) for path in directory.iterdir()
        if re.fullmatch(r"[0-9]+\.json", path.name)
    )
    blocks = []
    previous_file_hash = None
    previous_content_hash = GENESIS_PREV_HASH

    for expected_index, (index, path) in enumerate(candidates, start=1):
        if index != expected_index or path.name != f"{index}.json":
            raise LegacyChainError("Нарушена нумерация старых блоков.")

        raw = path.read_bytes()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise LegacyChainError(f"Некорректный JSON в блоке {index}.") from error

        if not isinstance(decoded, dict):
            raise LegacyChainError(f"Некорректный формат блока {index}.")

        text, timestamp, stored_index = (decoded.get(field) for field in ("text", "timestamp", "index"))
        if (
            not isinstance(text, str)
            or not _finite_number(timestamp)
            or not (type(stored_index) is int and stored_index == index or stored_index == str(index))
        ):
            raise LegacyChainError(f"Некорректные данные блока {index}.")
        if index > 1 and decoded.get("prev_hash") != previous_file_hash:
            raise LegacyChainError(f"Нарушена связь со старым блоком {index - 1}.")

        try:
            block = _new_block(index, float(timestamp), text, previous_content_hash)
        except (UnicodeError, ValueError) as error:
            raise LegacyChainError(f"Некорректные данные блока {index}.") from error
        blocks.append(block)
        previous_file_hash = hashlib.sha256(raw).hexdigest()
        previous_content_hash = block["content_hash"]

    return blocks


def _has_schema(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'blocks'"
    ).fetchone() is not None


def _initialize(connection, migrate_legacy, legacy_dir):
    if not _has_schema(connection):
        blocks = _read_legacy(legacy_dir or ROOT / "blocks") if migrate_legacy else []
        connection.execute(SCHEMA)
        for block in blocks or [_new_block(1, 0.0, "Genesis block", GENESIS_PREV_HASH)]:
            _insert(connection, block)


def init_db(db_path=None, migrate_legacy=True, legacy_dir=None) -> None:
    with _connection(db_path) as connection:
        if not _has_schema(connection):
            with _transaction(connection):
                _initialize(connection, migrate_legacy, legacy_dir)


def import_legacy_blocks(legacy_dir, db_path=None) -> int:
    blocks = _read_legacy(legacy_dir)
    with _connection(db_path) as connection, _transaction(connection):
        if _has_schema(connection):
            if connection.execute("SELECT 1 FROM blocks LIMIT 1").fetchone() is not None:
                raise ValueError("Импорт возможен только в пустую базу.")
        elif blocks:
            connection.execute(SCHEMA)
        for block in blocks:
            _insert(connection, block)
    return len(blocks)


def _read_chain(connection):
    return [
        dict(row) for row in connection.execute(
            "SELECT id, index_num AS 'index', timestamp, data AS text, prev_hash,"
            " content_hash, nonce, is_mined, mined_at FROM blocks ORDER BY index_num"
        )
    ]


def get_chain(db_path=None) -> list[dict]:
    init_db(db_path)
    with _connection(db_path) as connection:
        return _read_chain(connection)


def validate_chain(chain: list[dict], difficulty: int = DEFAULT_DIFFICULTY) -> list[dict]:
    if not chain:
        return [{"block": 1, "result": "error", "proof": -1, "reasons": ["Начальный блок отсутствует."]}]

    checks = []
    expected_previous = GENESIS_PREV_HASH
    for expected_index, block in enumerate(chain, start=1):
        index = block["index"]
        previous, digest = block["prev_hash"], block["content_hash"]
        text, timestamp = block["text"], block["timestamp"]
        nonce, mined, mined_at = block["nonce"], block["is_mined"], block["mined_at"]
        reasons = []
        if type(index) is not int or index != expected_index:
            reasons.append("Нарушен порядок блоков.")
        if previous != expected_previous:
            reasons.append("Хеш предыдущего блока не совпадает.")
        if (
            type(index) is not int or not isinstance(text, str) or not isinstance(previous, str)
            or not _finite_number(timestamp)
        ):
            reasons.append("Поля блока имеют некорректный формат.")
        elif digest != compute_content_hash(index, timestamp, text, previous):
            reasons.append("Хеш содержимого не совпадает.")

        if mined == 1:
            if (
                expected_index == 1 or not isinstance(digest, str)
                or not _finite_number(mined_at)
                or not is_valid_proof(digest, nonce, difficulty)
            ):
                reasons.append("Доказательство работы недействительно.")
        elif mined != 0 or nonce is not None or mined_at is not None:
            reasons.append("Статус доказательства работы не согласован.")

        checks.append({
            "block": index,
            "result": "error" if reasons else "ok",
            "proof": nonce if mined == 1 and nonce is not None else -1,
            "is_mined": mined,
            "reasons": reasons,
            "text": text,
        })
        expected_previous = digest
    return checks


def check_blocks_integrity(db_path=None, difficulty: int = DEFAULT_DIFFICULTY) -> list[dict]:
    return validate_chain(get_chain(db_path), difficulty)


def _verified_chain(connection):
    chain = _read_chain(connection)
    if any(result["result"] == "error" for result in validate_chain(chain)):
        raise CorruptChainError("Цепочка повреждена. Запись остановлена.")
    return chain


def write_block(text: str, make_proof: bool = False, db_path=None) -> dict:
    if not isinstance(text, str):
        raise TypeError("Данные блока должны быть строкой.")
    text = text.strip()
    if not text:
        raise ValueError("Поле данных не может быть пустым.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"В одном блоке может быть не больше {MAX_TEXT_LENGTH} символов.")
    try:
        text.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Данные должны содержать допустимый текст UTF-8.") from error

    with _connection(db_path) as connection, _transaction(connection):
        _initialize(connection, True, None)
        latest = _verified_chain(connection)[-1]
        block = _new_block(latest["index"] + 1, time(), text, latest["content_hash"], make_proof)
        _insert(connection, block)
        return block


def _mine_block(connection, block):
    nonce = _require_proof(block["content_hash"])
    connection.execute(
        "UPDATE blocks SET nonce = ?, is_mined = 1, mined_at = ? WHERE index_num = ?",
        (nonce, time(), block["index"]),
    )
    return nonce


def mine_pending_blocks(db_path=None, difficulty: int = DEFAULT_DIFFICULTY) -> int:
    if difficulty != DEFAULT_DIFFICULTY:
        raise ValueError("Сложность сохранённых блоков фиксирована.")
    with _connection(db_path) as connection, _transaction(connection):
        _initialize(connection, True, None)
        pending = [
            block for block in _verified_chain(connection)
            if block["index"] != 1 and not block["is_mined"]
        ][:MAX_MINING_BATCH]
        for block in pending:
            _mine_block(connection, block)
        return len(pending)


def _block_index(index_or_filename):
    return int(str(index_or_filename).removesuffix(".json"))


def get_next_block(db_path=None) -> str:
    chain = get_chain(db_path)
    if not chain:
        raise CorruptChainError("Начальный блок отсутствует.")
    return str(chain[-1]["index"] + 1)


def check_block(index: int, db_path=None) -> dict:
    index = _block_index(index)
    for check in check_blocks_integrity(db_path):
        if check["block"] == index:
            return check
    return {"block": index, "result": "error", "proof": -1, "reasons": ["Блок не найден."]}


def get_hash(index_or_name, db_path=None) -> str:
    index = _block_index(index_or_name)
    for block in get_chain(db_path):
        if block["index"] == index:
            return block["content_hash"]
    raise FileNotFoundError(f"Блок {index} не найден.")


def get_POW(block_index, difficulty: int = DEFAULT_DIFFICULTY, db_path=None) -> int | None:
    if difficulty != DEFAULT_DIFFICULTY:
        raise ValueError("Сложность сохранённых блоков фиксирована.")
    index = _block_index(block_index)
    with _connection(db_path) as connection, _transaction(connection):
        _initialize(connection, True, None)
        chain = _verified_chain(connection)
        if 1 < index <= len(chain):
            block = chain[index - 1]
            return block["nonce"] if block["is_mined"] else _mine_block(connection, block)
    return None
