# tests/test_search.py
import unittest
from app import create_app
from app.search import SearchEngine

class TestSearch(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        self.search_engine = SearchEngine()

    def test_search(self):
        # Тесты
