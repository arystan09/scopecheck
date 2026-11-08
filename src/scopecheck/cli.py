"""Command-line entry point; domain commands belong to later milestones."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scopecheck",
        description="ScopeCheck tool-authorization sandbox (foundation only).",
    )
    parser.parse_args()
    parser.print_help()
