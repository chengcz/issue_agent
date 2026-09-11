import asyncio
import sys

import pytest

from issue_agent.process import CommandError, run


@pytest.mark.parametrize("check", [False, True])
def test_closed_output_still_waits_for_failure(tmp_path, check):
    command = [
        sys.executable, "-c",
        "import os,time; os.close(1); os.close(2); time.sleep(0.2); os._exit(7)",
    ]
    if check:
        with pytest.raises(CommandError) as caught:
            asyncio.run(run(command, cwd=tmp_path))
        assert caught.value.result.returncode == 7
    else:
        assert asyncio.run(run(command, cwd=tmp_path, check=False)).returncode == 7


def test_closed_output_still_enforces_timeout(tmp_path):
    command = [
        sys.executable, "-c",
        "import os,time; os.close(1); os.close(2); time.sleep(60)",
    ]
    with pytest.raises(CommandError, match="timed out"):
        asyncio.run(run(command, cwd=tmp_path, timeout=1))


def test_cancellation_after_closed_output_terminates_child(tmp_path):
    ready = tmp_path / "ready"
    command = [
        sys.executable, "-c",
        ("import os,time,pathlib; os.close(1); os.close(2); "
         "pathlib.Path('ready').touch(); time.sleep(60)"),
    ]

    async def exercise():
        task = asyncio.create_task(run(command, cwd=tmp_path))
        try:
            async with asyncio.timeout(10):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
