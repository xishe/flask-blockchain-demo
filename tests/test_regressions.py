import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import blockchain
from server import create_app


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "chain.db"
        blockchain.init_db(self.db, migrate_legacy=False)
        app = create_app({"TESTING": True, "DATABASE_PATH": self.db})
        self.client = app.test_client()

    def test_damaged_chain_rejects_both_writes(self):
        self.client.post("/", data={"text": "Оригинал"})
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE blocks SET data = 'Подделка' WHERE index_num = 2")

        added = self.client.post("/", data={"text": "Лишний блок"})
        mined = self.client.post("/mining")

        self.assertEqual((added.status_code, mined.status_code), (409, 409))
        chain = blockchain.get_chain(self.db)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1]["text"], "Подделка")
        self.assertIsNone(chain[1]["nonce"])

    def test_explicit_import_preserves_existing_chain(self):
        self.client.post("/", data={"text": "Сохранить эту запись"})
        source = self.root / "legacy"
        source.mkdir()
        (source / "1.json").write_text(
            json.dumps({"text": "Alternative genesis", "timestamp": 0, "index": "1"}),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            blockchain.import_legacy_blocks(source, self.db)

        self.assertEqual(blockchain.get_chain(self.db)[1]["text"], "Сохранить эту запись")

    def test_malformed_legacy_fields_are_rejected(self):
        source = self.root / "legacy"
        source.mkdir()
        (source / "1.json").write_text(
            json.dumps({"text": ["not", "text"], "timestamp": 0, "index": "1"}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            blockchain.import_legacy_blocks(source, self.root / "legacy.db")

    def test_root_missing_is_reported_without_recreating_it(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM blocks")

        response = self.client.post("/check")

        self.assertIn("поврежд", response.get_data(as_text=True).lower())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], 0)

    def test_bad_database_is_reported_without_leaking_path(self):
        damaged_db = self.root / "local-private-path" / "damaged.db"
        damaged_db.parent.mkdir()
        damaged_db.write_text("not a database", encoding="utf-8")

        client = create_app({"TESTING": True, "DATABASE_PATH": damaged_db}).test_client()
        response = client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("local-private-path", response.get_data(as_text=True))
        self.assertEqual(damaged_db.read_text(encoding="utf-8"), "not a database")

    def test_request_size_is_bounded(self):
        response = self.client.post("/", data={"text": "a" * 128_000})
        self.assertEqual(response.status_code, 413)
