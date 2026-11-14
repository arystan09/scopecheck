"""Validate resolved production Compose without printing its secrets."""

import json
import re
import subprocess


def validate(config: dict) -> None:
    services = config["services"]
    images = {services[name]["image"] for name in ("app", "worker", "migrate")}
    if len(images) != 1 or not re.fullmatch(
        r"ghcr.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}", images.pop()
    ):
        raise ValueError("One immutable GHCR image digest required")
    for name in ("app", "worker", "migrate"):
        service = services[name]
        env = service["environment"]
        if (
            env.get("SCOPECHECK_PUBLIC_DEMO") != "true"
            or env.get("SCOPECHECK_MODE") != "fake"
        ):
            raise ValueError("Public fake mode required")
        if any("KEY" in key or "TOKEN" in key for key in env):
            raise ValueError("No provider key or telemetry token permitted")
        if set(service["networks"]) != {"private"} or service.get("ports"):
            raise ValueError("Application must remain private")
    if services["db"].get("ports") or not config["networks"]["private"]["internal"]:
        raise ValueError("Database network must remain private")


def main() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/compose.production.yaml",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit("Production Compose validation failed; check private env file")
    validate(json.loads(result.stdout))
    print("Production digest, fake mode and private network checks passed")


if __name__ == "__main__":
    main()
