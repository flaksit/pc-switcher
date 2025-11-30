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
    try:
        config = Config.load(config_path)
        # typer.echo(f"Starting sync to {target} with config from {config_path}")

        from pc_switcher.core.orchestrator import Orchestrator
        from pc_switcher.jobs.dummy import DummySuccessJob, DummyFailJob
        from pc_switcher.jobs.system.install import InstallOnTargetJob
        from pc_switcher.jobs.system.snapshots import BtrfsSnapshotJob
        from pc_switcher.jobs.background.disk_monitor import DiskSpaceMonitorJob

        orchestrator = Orchestrator(config, target)

        # Register jobs in order

        # 1. Background monitors (started by orchestrator or execute returns quickly)
        # Note: Our current implementation of DiskSpaceMonitorJob.execute starts a background task and returns.
        orchestrator.register_job(DiskSpaceMonitorJob("SOURCE"))
        orchestrator.register_job(DiskSpaceMonitorJob("TARGET"))

        # 2. Installation/Update
        orchestrator.register_job(InstallOnTargetJob())

        # 3. Pre-sync Snapshots
        orchestrator.register_job(BtrfsSnapshotJob(phase="presync"))

        # 4. Sync Jobs (Dummy for now)
        orchestrator.register_job(DummySuccessJob())
        # orchestrator.register_job(DummyFailJob()) # Uncomment to test failure

        # 5. Post-sync Snapshots
        orchestrator.register_job(BtrfsSnapshotJob(phase="postsync"))

        import asyncio

        asyncio.run(orchestrator.run())

    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


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
