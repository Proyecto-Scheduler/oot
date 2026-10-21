import logging
import shlex
import subprocess
from pathlib import Path

import click

import oot.commands as commands
from oot.commands.install import Action, resolvers
from oot.config import Project, load_project
from oot.errors import OotError

logger = logging.getLogger("oot")
pass_project = click.make_pass_decorator(Project)


class OotGroup(click.Group):
    """Turns expected failures into one-line errors; anything else keeps its traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (OotError, OSError) as e:
            raise click.ClickException(str(e))
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr
            raise click.ClickException(
                f"`{shlex.join(map(str, e.cmd))}` failed:\n{(stderr or '').strip()}"
            )


@click.group(cls=OotGroup)
@click.version_option("1.0")
@click.pass_context
@click.option(
    "--config",
    "-c",
    default="./oot.yml",
    help="Specify a custom config file",
    type=click.Path(),
)
@click.option("--verbose", "-v", count=True)
def cli(ctx, config, verbose):
    level = logging.WARNING

    if verbose == 1:
        level = logging.INFO
    if verbose >= 2:
        level = logging.DEBUG

    logging.basicConfig(level=level, format="%(message)s")

    config_path = Path(config)

    if not config_path.exists() and config_path == Path("./oot.yml"):
        raise click.ClickException(
            "No config file found.\n"
            "Expected default at ./oot.yml\n\n"
            "Use --config to specify a custom file."
        )

    ctx.obj = load_project(config_path)


@cli.command()
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="clone repos to specified directories, even if they're not empty",
)
@click.option(
    "--target",
    "-t",
    help="fetching target",
    default="all",
    type=click.Choice(["all", "kernel", "patches"]),
)
@pass_project
def fetch(project, force, target):
    targets = ["kernel", "patches"] if target == "all" else [target]
    for t in targets:
        commands.fetch(cfg=project, target=t, force=force, confirm=click.confirm)  # type: ignore


@cli.command()
@click.argument("target", type=click.Choice(["kernel", "patches"]))
@pass_project
def path(project, target):
    commands.path(project, target)


@cli.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.pass_context
@pass_project
def git(project, ctx):
    ctx.exit(commands.git(project, ctx.args))


@cli.command()
@click.option("--message", "-m", required=True, help="commit message for the patches repo")
@click.option(
    "--dry-run",
    is_flag=True,
    help="print the new metadata without writing or committing",
)
@pass_project
def sync(project, message, dry_run):
    commands.sync(project, message, dry_run=dry_run)


@cli.command()
@click.option("--dry-run", "-dr", is_flag=True)
@click.option(
    "--fail-fast",
    "-ff",
    is_flag=True,
    help="if passed, fail and exit after the first error",
)
@click.option(
    "--on-conflict",
    help="default action to take when a conflict is detected",
    type=click.Choice([a.value for a in Action]),
)
@click.option("--metadata_path", "-m", help="custom path for metadata file")
@pass_project
def install(project, dry_run, fail_fast, on_conflict, metadata_path):
    resolver = (
        resolvers[on_conflict] if on_conflict is not None else prompt_resolver
    )

    commands.install(
        cfg=project,
        dry_run=dry_run,
        fail_fast=fail_fast,
        metadata_path=metadata_path,
        resolver=resolver,
    )


def prompt_resolver(path: str) -> Action:
    click.echo(f"\n  ! {path} has local changes on top of the applied patch.")
    return Action(
        click.prompt(
            "    What do you want to do?",
            type=click.Choice([a.value for a in Action]),
            default=Action.ABORT.value,
        )
    )
