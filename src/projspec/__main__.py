#!/usr/bin/env python
"""Simple example executable for this library"""

import json

import click

import projspec.proj


@click.command()
@click.option(
    "--types",
    default="ALL",
    help='Type names to scan for (comma-separated list in camel or snake case); defaults to "ALL"',
)
@click.argument("path", default=".")
@click.option(
    "--walk", is_flag=True, help="To descend into all child directories"
)
@click.option(
    "--storage_options",
    default="",
    help="storage options dict for the given URL, as JSON",
)
def main(path, types, walk, storage_options):
    if types in {"ALL", ""}:
        types = None
    else:
        types = types.split(",")
    if storage_options:
        storage_options = json.loads(storage_options)
    else:
        storage_options = None
    proj = projspec.Project(
        path, storage_options=storage_options, types=types, walk=walk
    )
    print(proj)


if __name__ == "__main__":
    main()
