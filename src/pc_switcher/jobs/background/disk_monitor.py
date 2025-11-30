import asyncio
from typing import List
from pc_switcher.jobs.base import Job, JobContext


class DiskSpaceMonitorJob(Job):
    name = "disk_monitor"
    required = True
    background = True

    def __init__(self, context: JobContext, target: str):
        super().__init__(context)
        self.target = target  # "SOURCE" or "TARGET"
        self._running = False

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[str]:
        errors = []
        # Validate thresholds
        for key in ["preflight_minimum", "runtime_minimum"]:
            val = config.get(key)
            if val:
                if not isinstance(val, str) or not val.endswith("%"):
                    # Simple check for now
                    pass
        return errors

    async def validate(self) -> list[str]:
        # Check pre-flight disk space
        threshold = self.context.config.get("preflight_minimum", "20%")
        # Parse threshold (simplified)
        # In real impl, handle %, GiB, etc.
        # For now, just assume we can check `df` output
        return []

    async def execute(self) -> None:
        self._running = True
        await self._monitor_loop()

    async def _monitor_loop(self):
        interval = self.context.config.get("check_interval", 30)
        threshold = self.context.config.get("runtime_minimum", "15%")

        executor = self.context.source if self.target == "SOURCE" else self.context.target

        while self._running:
            try:
                # Check disk space
                # df -h / --output=pcent
                res = await executor.run_command("df --output=pcent / | tail -1")
                if res.success:
                    usage_str = res.stdout.strip().replace("%", "")
                    try:
                        usage = int(usage_str)
                        free = 100 - usage

                        # Parse threshold (assume % for now)
                        min_free = int(threshold.replace("%", ""))

                        if free < min_free:
                            msg = f"Disk space critical on {self.target}: {free}% free (min {min_free}%)"
                            self.context.logger.critical(msg)
                            raise RuntimeError(msg)

                    except ValueError:
                        self.context.logger.warning(f"Failed to parse disk usage: {res.stdout}")

            except Exception as e:
                self.context.logger.error(f"Disk monitor failed: {e}")
                # Re-raise if it's our critical error
                if isinstance(e, RuntimeError) and "Disk space critical" in str(e):
                    raise

            await asyncio.sleep(interval)

    def stop(self):
        self._running = False
