import asyncio
import asyncssh

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    async def run_command(self, cmd: str, timeout: float | None = None) -> CommandResult:
        pass

    @abstractmethod
    async def put_file(self, local_path: str, remote_path: str) -> bool:
        pass


class LocalExecutor(Executor):
    async def run_command(self, cmd: str, timeout: float | None = None) -> CommandResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
                return CommandResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout.decode(),
                    stderr=stderr.decode(),
                    success=proc.returncode == 0,
                )
            except asyncio.TimeoutError:
                proc.terminate()
                return CommandResult(-1, "", "Timeout", False)
        except Exception as e:
            return CommandResult(-1, "", str(e), False)

    async def put_file(self, local_path: str, remote_path: str) -> bool:
        try:
            await asyncio.to_thread(shutil.copy2, local_path, remote_path)
            return True
        except Exception:
            return False


class Connection:
    _target: str
    _event_bus: EventBus
    _conn: asyncssh.SSHClientConnection | None

    def __init__(self, target: str, event_bus: EventBus):
        self._target = target
        self._event_bus = event_bus
        self._conn = None

    async def connect(self) -> None:
        if self._target == "local":
            self._event_bus.publish(ConnectionEvent(status="Connected (Local Mode)"))
            return

        try:
            self._conn = await asyncssh.connect(self._target)
            self._event_bus.publish(ConnectionEvent(status="Connected"))
        except Exception as e:
            self._event_bus.publish(ConnectionEvent(status=f"Error: {e}"))
            raise

    async def close(self) -> None:
        if self._target == "local":
            self._event_bus.publish(ConnectionEvent(status="Disconnected (Local Mode)"))
            return

        if self._conn:
            self._conn.close()
            self._event_bus.publish(ConnectionEvent(status="Disconnected"))

    def get_executor(self) -> Executor:
        if self._target == "local":
            return LocalExecutor()
        return RemoteExecutor(self)


class RemoteExecutor(Executor):
    _connection: Connection

    def __init__(self, connection: Connection):
        self._connection = connection

    async def run_command(self, cmd: str, timeout: float | None = None) -> CommandResult:
        if not self._connection._conn:
            return CommandResult(-1, "", "Not connected", False)

        try:
            result = await self._connection._conn.run(cmd, timeout=timeout)
            # asyncssh result attributes might be None or bytes/str depending on encoding
            # We assume default encoding (utf-8) so they are strings
            return CommandResult(
                exit_code=result.exit_status or 0,
                stdout=str(result.stdout),
                stderr=str(result.stderr),
                success=result.exit_status == 0,
            )
        except Exception as e:
            return CommandResult(-1, "", str(e), False)

    async def put_file(self, local_path: str, remote_path: str) -> bool:
        if not self._connection._conn:
            return False
        try:
            await asyncssh.scp(local_path, (self._connection._conn, remote_path))
            return True
        except Exception:
            return False
