import asyncio
from typing import List, Dict, Any
from pc_switcher.jobs.base import Job, JobContext


class DummySuccessJob(Job):
    name = "dummy_success"

    async def validate(self) -> List[str]:
        self.context.logger.info("Validating DummySuccessJob...")
        return []

    async def execute(self) -> None:
        self.context.logger.info("Starting DummySuccessJob")

        # Simulate work on source
        self.context.logger.info("Working on source...")
        for i in range(5):
            await asyncio.sleep(1)
            self.context.logger.debug(f"Source step {i + 1}/5")
            # Emit progress via event bus (hack: accessing protected member for now, or we need to pass event bus to context)
            # Since we didn't add event_bus to JobContext, let's use the logger to emit a specific log that UI might parse?
            # No, the architecture says Job -> report_progress -> UI.
            # But we didn't implement report_progress fully in base.
            # Let's just log for now.
            self.context.logger.info(f"Progress: {i * 20}%")

        # Simulate work on target
        self.context.logger.info("Working on target...")
        res = await self.context.target.run_command("echo 'Hello from target'", timeout=5.0)
        if res.success:
            self.context.logger.info(f"Target said: {res.stdout.strip()}")
        else:
            self.context.logger.error(f"Target failed: {res.stderr}")

        self.context.logger.info("DummySuccessJob finished")


class DummyFailJob(Job):
    name = "dummy_fail"

    async def validate(self) -> List[str]:
        return []

    async def execute(self) -> None:
        self.context.logger.info("Starting DummyFailJob")
        await asyncio.sleep(2)
        self.context.logger.warning("Something looks suspicious...")
        await asyncio.sleep(2)
        self.context.logger.error("Oh no, failure imminent!")
        raise RuntimeError("Simulated failure in DummyFailJob")
