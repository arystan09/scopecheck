"""Independent Python processes with a pipe barrier, never a shared SQL session."""

import asyncio
import json
import os
import sys


async def race_processes(database_url, program, payloads):
    env = {**os.environ, "SCOPECHECK_DATABASE_URL": database_url}
    env.pop("SCOPECHECK_OPENAI_API_KEY", None)
    processes = []
    try:
        for _ in payloads:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                program,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
        for process in processes:
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        # Both contenders have started before either receives its work.
        for process, payload in zip(processes, payloads, strict=True):
            process.stdin.write((json.dumps(payload, default=str) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
        results = []
        for process in processes:
            output = await asyncio.wait_for(process.stdout.read(), 15)
            await process.wait()
            assert process.returncode == 0, "Independent database contender failed"
            results.append(json.loads(output))
        return results
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
