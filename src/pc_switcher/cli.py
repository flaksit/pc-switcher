import typer
from pathlib import Path
from typing import Optional
from pc_switcher.config import Config, DEFAULT_CONFIG_PATH

app = typer.Typer(help="PC-switcher: Seamless synchronization between Linux machines.")


@app.command()
def sync(
    target: str = typer.Argument(..., help="Target machine hostname or alias"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to configuration file"),
):
    """
    Synchronize the current machine (source) with the target machine.
    """
    import asyncio
    from pc_switcher.core.orchestrator import Orchestrator
    from pc_switcher.jobs.dummy import DummySuccessJob, DummyFailJob
    from pc_switcher.jobs.system.install import InstallOnTargetJob
    from pc_switcher.jobs.system.snapshots import BtrfsSnapshotJob
    from pc_switcher.jobs.background.disk_monitor import DiskSpaceMonitorJob

    async def main_sync(target: str, config_path: Path):
        try:
            config = await Config.load(config_path)
            # typer.echo(f"Starting sync to {target} with config from {config_path}")

            # Initialize Orchestrator
            # event_bus, ui, connection are not defined here because they are created inside Orchestrator?
            # No, Orchestrator takes them as arguments.
            # We need to create them here or Orchestrator should create them?
            # The previous code (before my edit) didn't have them.
            # Let's check how it was before.
            # Before: orchestrator = Orchestrator(config, target)
            # And Orchestrator created them.
            # But I changed Orchestrator init to take them.
            # Why? Because I wanted to pass config_path.
            # But I also added event_bus, ui, connection to init args in my previous edit to orchestrator.py?
            # Let's check orchestrator.py init signature again.
            # Yes: def __init__(self, config: Config, config_path: Path, target_host: str, event_bus: EventBus, ui: TerminalUI, connection: Connection):
            # This was a mistake. Orchestrator should create them or they should be created in CLI.
            # Ideally CLI creates them to handle dependency injection.
            # So I should create them in CLI.

            from pc_switcher.core.events import EventBus
            from pc_switcher.core.ui import TerminalUI
            from pc_switcher.core.connection import Connection

            event_bus = EventBus()
            ui = TerminalUI(event_bus, config.global_settings.log_cli_level)
            connection = Connection(target, event_bus)

            orchestrator = Orchestrator(config, config_path, target, event_bus, ui, connection)

            # Register jobs in order

            # 1. Background monitors (started by orchestrator or execute returns quickly)
            # Note: Our current implementation of DiskSpaceMonitorJob.execute starts a background task and returns.
            orchestrator.register_job(DiskSpaceMonitorJob, target="SOURCE")
            orchestrator.register_job(DiskSpaceMonitorJob, target="TARGET")

            # 2. Installation/Update
            orchestrator.register_job(InstallOnTargetJob)

            # 3. Pre-sync Snapshots
            orchestrator.register_job(BtrfsSnapshotJob, phase="presync")

            # 4. Sync Jobs (Dummy for now)
            orchestrator.register_job(DummySuccessJob)
            # orchestrator.register_job(DummyFailJob) # Uncomment to test failure

            # 5. Post-sync Snapshots
            orchestrator.register_job(BtrfsSnapshotJob, phase="postsync")

            await orchestrator.run()

        except Exception as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1)

    asyncio.run(main_sync(target, config_path))


@app.command()
def logs(
    last: bool = typer.Option(False, "--last", help="Show the most recent log file"),
):
    """
    View sync logs.
    """
    typer.echo("Log viewing not implemented yet.")


@app.command()
def cleanup_snapshots(
    older_than: str = typer.Option("7d", help="Delete snapshots older than this duration"),
):
    """
    Cleanup old btrfs snapshots.
    """
    typer.echo(f"Cleaning up snapshots older than {older_than}...")


if __name__ == "__main__":
    app()
