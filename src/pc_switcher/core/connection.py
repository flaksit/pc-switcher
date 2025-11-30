import asyncio
import asyncssh
from typing import Optional, Tuple, AsyncIterator
from abc import ABC, abstractmethod
from dataclasses import dataclass
import subprocess
import shutil

from pc_switcher.core.events import EventBus, ConnectionEvent


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    success: bool


class Executor(ABC):
    @abstractmethod
    async def run_command(self, cmd: str, timeout: Optional[float] = None) -> CommandResult:
        pass


class LocalExecutor(Executor):
    async def run_command(self, cmd: str, timeout: Optional[float] = None) -> CommandResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
                return CommandResult(
                    exit_code=proc.returncode,
                    stdout=stdout.decode(),
                    stderr=stderr.decode(),
                    success=proc.returncode == 0,
                )
            except asyncio.TimeoutError:
                proc.terminate()
                return CommandResult(-1, "", "Timeout", False)
        except Exception as e:
            return CommandResult(-1, "", str(e), False)


class Connection:
    def __init__(self, target: str, event_bus: EventBus):
        self._target = target
        self._event_bus = event_bus
        self._conn: Optional[asyncssh.SSHClientConnection] = None

    async def connect(self) -> None:
        try:
            self._conn = await asyncssh.connect(self._target)
            self._event_bus.publish(ConnectionEvent("Connected"))
        except Exception as e:
            self._event_bus.publish(ConnectionEvent(f"Error: {e}"))
            raise

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._event_bus.publish(ConnectionEvent("Disconnected"))

    def get_executor(self) -> "RemoteExecutor":
        return RemoteExecutor(self)


class RemoteExecutor(Executor):
    def __init__(self, connection: Connection):
        self._connection = connection

    async def run_command(self, cmd: str, timeout: Optional[float] = None) -> CommandResult:
        if not self._connection._conn:
            return CommandResult(-1, "", "Not connected", False)

        try:
            result = await self._connection._conn.run(cmd, timeout=timeout)
            return CommandResult(
                exit_code=result.exit_status,
                stdout=result.stdout,
                stderr=result.stderr,
                success=result.exit_status == 0,
            )
        except Exception as e:
            return CommandResult(-1, "", str(e), False)
