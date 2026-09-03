from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int | None = None
    usage: dict[str, Any] | None = field(default=None, compare=False)


async def run(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    timeout: int = 3600,
    stdin: str | None = None,
    check: bool = True,
) -> Result:
    log.info("run cwd=%s command=%s", cwd, command[0])
    started = time.monotonic()
    process_options = {"start_new_session": True} if os.name != "nt" else {}
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_options,
    )

    async def terminate_process_tree() -> None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await killer.communicate()
            if process.returncode is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except TimeoutError:
        await terminate_process_tree()
        raise CommandError(f"command timed out after {timeout}s: {command[0]}") from None
    except asyncio.CancelledError:
        await terminate_process_tree()
        raise
    result = Result(
        process.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if check and result.returncode:
        tail = (result.stderr or result.stdout)[-4000:]
        raise CommandError(f"command failed ({result.returncode}): {command[0]}\n{tail}")
    return result


async def shell(
    command: str, *, cwd: Path, timeout: int = 3600, check: bool = True
) -> Result:
    shell_command = ("cmd.exe", "/d", "/s", "/c", command) if os.name == "nt" else ("/bin/sh", "-lc", command)
    return await run(shell_command, cwd=cwd, timeout=timeout, check=check)
