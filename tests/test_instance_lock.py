import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from issue_agent.orchestrator import Orchestrator


def app_for(path):
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(state_db=path)
    return app


CHILD = """
import sys
from pathlib import Path
from types import SimpleNamespace
from issue_agent.orchestrator import Orchestrator
app = Orchestrator.__new__(Orchestrator)
app.config = SimpleNamespace(state_db=Path(sys.argv[1]))
print('ready', flush=True)
input()
print(app.acquire_instance_lock(), flush=True)
input()
app.release_instance_lock()
"""


def start_child(path):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    return subprocess.Popen(
        [sys.executable, "-u", "-c", CHILD, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )


def test_simultaneous_processes_have_one_lock_owner(tmp_path):
    children = [start_child(tmp_path / "state.db") for _ in range(4)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.stdout.readline().strip() for child in children]
        assert results.count("True") == 1
        assert results.count("False") == 3
        for child in children:
            child.stdin.write("release\n")
            child.stdin.flush()
            assert child.wait(timeout=10) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_crashed_owner_releases_lock_without_removing_file(tmp_path):
    path = tmp_path / "state.db"
    child = start_child(path)
    app = app_for(path)
    try:
        assert child.stdout.readline().strip() == "ready"
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "True"
        assert not app.acquire_instance_lock()
        child.kill()
        child.communicate(timeout=10)
        assert app.acquire_instance_lock()
        app.release_instance_lock()
        assert path.with_suffix(".db.lock").exists()
        assert app.acquire_instance_lock()
    finally:
        app.release_instance_lock()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
