from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


async def run(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    timeout: int = 3600,
    stdin: str | None = None,
    check: bool = True,
) -> Result:
    log.info("run cwd=%s command=%s", cwd, command[0])
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise CommandError(f"command timed out after {timeout}s: {command[0]}") from None
    result = Result(process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))
    if check and result.returncode:
        tail = (result.stderr or result.stdout)[-4000:]
        raise CommandError(f"command failed ({result.returncode}): {command[0]}\n{tail}")
    return result


async def shell(
    command: str, *, cwd: Path, timeout: int = 3600, check: bool = True
) -> Result:
    shell_command = ("cmd.exe", "/d", "/s", "/c", command) if os.name == "nt" else ("/bin/sh", "-lc", command)
    return await run(shell_command, cwd=cwd, timeout=timeout, check=check)
