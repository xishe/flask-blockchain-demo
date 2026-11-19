import os
import shutil
import sqlite3
import tempfile
import unittest

import blockchain


class TestBlockchain(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_blockchain.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_genesis_initialization(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 1)
        genesis = chain[0]
        self.assertEqual(genesis["index"], 1)
        self.assertEqual(genesis["text"], "Genesis block")
        self.assertTrue(len(genesis["content_hash"]) == 64)
        self.assertIn("prev_hash", genesis)

    def test_append_block_and_persistence(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        new_block = blockchain.write_block("Первая запись", make_proof=False, db_path=self.db_path)
        self.assertEqual(new_block["index"], 2)
        self.assertEqual(new_block["text"], "Первая запись")
        self.assertEqual(new_block["is_mined"], 0)

        # Re-query directly to confirm persistence
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1]["text"], "Первая запись")
        self.assertEqual(chain[1]["prev_hash"], chain[0]["content_hash"])

    def test_mining_does_not_change_content_hash(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        b2 = blockchain.write_block("Блок для майнинга", make_proof=False, db_path=self.db_path)
        original_hash = b2["content_hash"]
        original_prev_hash = b2["prev_hash"]

        # Mine pending blocks
        mined_count = blockchain.mine_pending_blocks(db_path=self.db_path)
        self.assertEqual(mined_count, 1)

        chain = blockchain.get_chain(self.db_path)
        mined_b2 = chain[1]
        self.assertEqual(mined_b2["is_mined"], 1)
        self.assertIsNotNone(mined_b2["nonce"])
        # Crucial requirement: content hash and prev_hash must NOT change
        self.assertEqual(mined_b2["content_hash"], original_hash)
        self.assertEqual(mined_b2["prev_hash"], original_prev_hash)

        # Nonce must satisfy PoW condition
        self.assertTrue(blockchain.is_valid_proof(mined_b2["content_hash"], mined_b2["nonce"]))

    def test_idempotent_mining(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        blockchain.write_block("Блок 2", make_proof=False, db_path=self.db_path)

        count_first = blockchain.mine_pending_blocks(db_path=self.db_path)
        self.assertEqual(count_first, 1)

        # Second mining call: should mine 0 blocks and leave state intact
        count_second = blockchain.mine_pending_blocks(db_path=self.db_path)
        self.assertEqual(count_second, 0)

        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1]["is_mined"], 1)

    def test_tampering_detection_without_mutation(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        blockchain.write_block("Настоящие данные", make_proof=False, db_path=self.db_path)

        # Directly tamper with SQLite data
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE blocks SET data = 'Подделка' WHERE index_num = 2")
        conn.commit()
        conn.close()

        results = blockchain.check_blocks_integrity(db_path=self.db_path)
        # Check should report error
        b2_result = next((r for r in results if r["block"] == 2 or r["block"] == "2"), None)
        self.assertIsNotNone(b2_result)
        self.assertEqual(b2_result["result"], "error")

        # Crucial requirement: check must not mutate or overwrite the corrupted state
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT data FROM blocks WHERE index_num = 2").fetchone()
        conn.close()
        self.assertEqual(row[0], "Подделка")

    def test_legacy_import_valid_chain(self):
        legacy_dir = os.path.join(self.test_dir, "legacy_blocks")
        os.makedirs(legacy_dir, exist_ok=True)

        # Create 1.json
        p1 = os.path.join(legacy_dir, "1.json")
        with open(p1, "wb") as f:
            f.write(b'{\n    "text": "Genesis block",\n    "hash": "",\n    "timestamp": 0,\n    "proof": -1,\n    "index": "1"\n}')

        import hashlib
        with open(p1, "rb") as f:
            h1 = hashlib.sha256(f.read()).hexdigest()

        # Create 2.json with prev_hash matching raw 1.json
        p2 = os.path.join(legacy_dir, "2.json")
        b2_json = (
            '{\n'
            '    "text": "Old block 2",\n'
            f'    "prev_hash": "{h1}",\n'
            '    "timestamp": 123456.789,\n'
            '    "proof": 42,\n'
            '    "index": "2"\n'
            '}'
        )
        with open(p2, "w", encoding="utf-8") as f:
            f.write(b2_json)

        imported = blockchain.import_legacy_blocks(legacy_dir, db_path=self.db_path)
        self.assertEqual(imported, 2)

        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]["text"], "Genesis block")
        self.assertEqual(chain[0]["timestamp"], 0.0)
        self.assertEqual(chain[1]["text"], "Old block 2")
        self.assertEqual(chain[1]["timestamp"], 123456.789)
        # Old proof is NOT carried over because old formula is invalid in new model
        self.assertEqual(chain[1]["is_mined"], 0)

        # Original files must be preserved
        self.assertTrue(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))

    def test_legacy_import_tampered_prev_hash_aborts_without_mutation(self):
        legacy_dir = os.path.join(self.test_dir, "legacy_corrupt")
        os.makedirs(legacy_dir, exist_ok=True)

        p1 = os.path.join(legacy_dir, "1.json")
        with open(p1, "wb") as f:
            f.write(b'{"text": "Genesis", "index": "1", "timestamp": 0}')

        # 2.json with bad prev_hash
        p2 = os.path.join(legacy_dir, "2.json")
        with open(p2, "w", encoding="utf-8") as f:
            f.write('{"text": "Bad", "prev_hash": "wronghash", "index": "2", "timestamp": 1}')

        with self.assertRaises(Exception):
            blockchain.import_legacy_blocks(legacy_dir, db_path=self.db_path)

        # Original files intact
        self.assertTrue(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))

    def test_legacy_import_non_contiguous_sequence_rejected(self):
        legacy_dir = os.path.join(self.test_dir, "legacy_gap")
        os.makedirs(legacy_dir, exist_ok=True)

        p1 = os.path.join(legacy_dir, "1.json")
        with open(p1, "wb") as f:
            f.write(b'{"text": "Genesis", "index": "1", "timestamp": 0}')

        p3 = os.path.join(legacy_dir, "3.json")
        with open(p3, "wb") as f:
            f.write(b'{"text": "Gap", "index": "3", "timestamp": 1}')

        with self.assertRaises(ValueError):
            blockchain.import_legacy_blocks(legacy_dir, db_path=self.db_path)

    def test_typed_input_validation_in_write_block(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)

        with self.assertRaises(TypeError):
            blockchain.write_block(12345, db_path=self.db_path)

        with self.assertRaises(TypeError):
            blockchain.write_block(None, db_path=self.db_path)

        with self.assertRaises(ValueError):
            blockchain.write_block("", db_path=self.db_path)

        with self.assertRaises(ValueError):
            blockchain.write_block("   \t\n  ", db_path=self.db_path)

        with self.assertRaises(ValueError):
            blockchain.write_block("X" * (blockchain.MAX_TEXT_LENGTH + 1), db_path=self.db_path)

    def test_bounded_proof_logic(self):
        c_hash = "abcde" * 12 + "1234"
        self.assertFalse(blockchain.is_valid_proof(c_hash, None))
        self.assertFalse(blockchain.is_valid_proof(c_hash, -5))
        self.assertFalse(blockchain.is_valid_proof(c_hash, "not-an-int"))

        # Bounded search with 0 iterations must return None
        self.assertIsNone(blockchain.mine_hash(c_hash, difficulty=4, max_iterations=0))

    def test_tampering_prev_hash_and_nonce_detected(self):
        blockchain.init_db(self.db_path, migrate_legacy=False)
        blockchain.write_block("Блок для проверки подделок", make_proof=True, db_path=self.db_path)

        # 1. Tamper prev_hash
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE blocks SET prev_hash = 'bad_hash' WHERE index_num = 2")
        conn.commit()
        conn.close()

        res1 = blockchain.check_blocks_integrity(self.db_path)
        b2_res = next(r for r in res1 if r["block"] == 2)
        self.assertEqual(b2_res["result"], "error")

        # 2. Restore prev_hash and tamper nonce
        chain = blockchain.get_chain(self.db_path)
        correct_prev = chain[0]["content_hash"]
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE blocks SET prev_hash = ?, nonce = 999999999 WHERE index_num = 2", (correct_prev,))
        conn.commit()
        conn.close()

        res2 = blockchain.check_blocks_integrity(self.db_path)
        b2_res2 = next(r for r in res2 if r["block"] == 2)
        self.assertEqual(b2_res2["result"], "error")

    def test_concurrency_overlapping_writes(self):
        import concurrent.futures
        blockchain.init_db(self.db_path, migrate_legacy=False)

        def worker(i):
            return blockchain.write_block(f"Параллельный блок {i}", make_proof=False, db_path=self.db_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), 10)
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 11)  # Genesis + 10 blocks

        # Verify sequential indices 1..11
        indices = [b["index"] for b in chain]
        self.assertEqual(indices, list(range(1, 12)))

        # Verify hash link integrity
        integrity = blockchain.check_blocks_integrity(self.db_path)
        for check in integrity:
            self.assertEqual(check["result"], "ok")


if __name__ == "__main__":
    unittest.main()
