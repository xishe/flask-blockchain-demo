import os
import shutil
import sqlite3
import tempfile
import unittest

import server
import blockchain


class TestServer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_server.db")

        # Initialize test database
        blockchain.init_db(self.db_path, migrate_legacy=False)

        # Create Flask test client
        self.app = server.create_app({
            "TESTING": True,
            "DATABASE_PATH": self.db_path,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_index_renders_chain_russian_no_cdn(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        # Verify Russian UI elements
        self.assertIn("Genesis block", html)
        self.assertIn("Добавить", html)
        self.assertIn("Цепочка блоков", html)

        # Verify no external CDN or framework scripts/styles
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("bootstrapcdn", html)
        self.assertNotIn("cdnfonts.com", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

        # Semantic accessibility tags
        self.assertIn("<main", html)
        self.assertIn("<form", html)
        self.assertIn("<label", html)

    def test_post_empty_or_whitespace_text_validation(self):
        # Empty text
        r1 = self.client.post("/", data={"text": ""})
        # Should be rejected with error or status 400
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 1)  # Only genesis

        # Whitespace-only text
        r2 = self.client.post("/", data={"text": "    \n\t  "})
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 1)

    def test_post_overly_long_text_validation(self):
        long_text = "A" * 6000
        response = self.client.post("/", data={"text": long_text})
        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 1)  # Only genesis

    def test_post_valid_block_append_and_redirect(self):
        response = self.client.post("/", data={"text": "Новая запись в реестре", "make_proof": "on"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Новая запись в реестре", html)

        chain = blockchain.get_chain(self.db_path)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1]["is_mined"], 1)

    def test_xss_escaping(self):
        xss_payload = "<script>alert('xss')</script>"
        self.client.post("/", data={"text": xss_payload}, follow_redirects=True)

        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertNotIn("<script>alert('xss')</script>", html)
        self.assertIn("&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", html)

    def test_post_check_route(self):
        # Add block
        self.client.post("/", data={"text": "Блок для проверки"}, follow_redirects=True)

        response = self.client.post("/check")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Проверка целостности", html)
        self.assertIn("Целостность подтверждена", html)

    def test_post_mining_route_idempotent(self):
        # Add block without proof
        self.client.post("/", data={"text": "Блок без proof"}, follow_redirects=True)

        # First mining call
        r1 = self.client.post("/mining")
        self.assertEqual(r1.status_code, 200)
        html1 = r1.get_data(as_text=True)
        self.assertIn("Майнинг", html1)

        # Second mining call: idempotent
        r2 = self.client.post("/mining")
        self.assertEqual(r2.status_code, 200)
        html2 = r2.get_data(as_text=True)
        self.assertIn("Ожидающих блоков: 0.", html2)

    def test_corrupt_state_handling_without_mutation(self):
        # Add block
        self.client.post("/", data={"text": "Оригинал"}, follow_redirects=True)

        # Tamper directly in SQLite
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE blocks SET data = 'Испорчено' WHERE index_num = 2")
        conn.commit()
        conn.close()

        # POST /check should detect corruption
        response = self.client.post("/check")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("поврежд", html.lower())

        # Verify data in DB was not overwritten
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT data FROM blocks WHERE index_num = 2").fetchone()
        conn.close()
        self.assertEqual(row[0], "Испорчено")


if __name__ == "__main__":
    unittest.main()
