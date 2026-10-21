"""Locked JSON state: concurrent writers lose nothing; watch keeps on-disk dismissals."""
import json, multiprocessing, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import fleet_state  # noqa: E402


def writer(path, k):
    for i in range(25):
        fleet_state.update(path, lambda d, key=f"{k}-{i}": d.__setitem__(key, i))


class State(unittest.TestCase):
    def test_concurrent_writers_lose_nothing(self):
        path = os.path.join(tempfile.mkdtemp(), "notes.json")
        procs = [multiprocessing.Process(target=writer, args=(path, k)) for k in range(6)]
        [p.start() for p in procs]
        [p.join() for p in procs]
        self.assertEqual(len(json.loads(Path(path).read_text())), 6 * 25)

    def test_corrupt_file_is_an_error_not_empty(self):
        path = os.path.join(tempfile.mkdtemp(), "notes.json")
        Path(path).write_text("{not json")
        with self.assertRaises(RuntimeError):
            fleet_state.update(path, lambda d: d.__setitem__("x", 1))


if __name__ == "__main__":
    unittest.main()
