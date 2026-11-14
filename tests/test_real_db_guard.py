"""Tests can never open the owner's real data/jobcopilot.db (see store._refuse_real_db_under_tests)."""
import pytest

from jobcopilot import store


def test_connect_refuses_the_real_database_under_pytest():
    real = store._REAL_DATA_DIR / "jobcopilot.db"
    with pytest.raises(RuntimeError, match="real database"):
        with store.connect(real):
            pass


def test_temp_databases_still_work(tmp_path):
    with store.connect(tmp_path / "x.db") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
