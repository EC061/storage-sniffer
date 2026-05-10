from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from prettytable import PrettyTable
from tqdm import tqdm


BYTES_IN_BLOCK = 512
MIB = 1024**2
GIB = 1024**3
TIB = 1024**4


@dataclass(frozen=True)
class Container:
    container_id: str
    image: str
    name: str
    status: str


@dataclass(frozen=True)
class UserUsage:
    container_name: str
    user: str
    bytes_used: int
    error: str | None = None


@dataclass(frozen=True)
class ContainerReport:
    container: Container
    usages: list[UserUsage]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Docker container and per-user storage usage."
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Run docker directly instead of through sudo.",
    )
    parser.add_argument(
        "--container",
        action="append",
        default=[],
        help=(
            "Limit output to one container name or id. May be provided multiple "
            "times; by default all running containers are counted."
        ),
    )
    parser.add_argument(
        "--output",
        default="storage-usage-report.md",
        help="Markdown report path to write as containers finish scanning.",
    )
    parser.add_argument(
        "--history",
        default="storage-usage-history.jsonl",
        help="JSON lines file path to read past runs and append current run history.",
    )
    return parser.parse_args()


def format_size(num_bytes: int) -> str:
    if num_bytes >= TIB:
        return f"{num_bytes / TIB:.2f} TB"
    if num_bytes >= GIB:
        return f"{num_bytes / GIB:.2f} GB"
    return f"{num_bytes / MIB:.2f} MB"


def format_trend(current_bytes: int, previous_bytes: int | None) -> str:
    if previous_bytes is None:
        return ""
    diff = current_bytes - previous_bytes
    if diff == 0:
        return ""
    sign = "+" if diff > 0 else "-"
    size_str = format_size(abs(diff))
    arrow = "↑" if diff > 0 else "↓"
    return f" {arrow} {sign}{size_str}"


def load_previous_history(history_path: Path) -> dict[str, Any] | None:
    if not history_path.exists():
        return None
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                return json.loads(line)
    except Exception as exc:
        print(f"Warning: could not read history file {history_path}: {exc}", file=sys.stderr)
    return None


def save_history(history_path: Path, reports: list[ContainerReport]) -> None:
    if not reports:
        return
    data: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "containers": {},
        "grand_total_bytes": 0,
    }
    for report in reports:
        container_bytes = sum(u.bytes_used for u in report.usages if u.error is None)
        data["grand_total_bytes"] += container_bytes
        users_data = {
            u.user: u.bytes_used
            for u in report.usages if u.error is None
        }
        data["containers"][report.container.name] = {
            "total_bytes": container_bytes,
            "users": users_data,
        }
    try:
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")
    except Exception as exc:
        print(f"Error saving history to {history_path}: {exc}", file=sys.stderr)


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def docker_command(args: Iterable[str], use_sudo: bool) -> list[str]:
    command = ["docker", *args]
    if use_sudo:
        return ["sudo", *command]
    return command


def ensure_sudo_authenticated(use_sudo: bool) -> bool:
    if not use_sudo:
        return True

    print("Docker access uses sudo; enter your password if prompted.", file=sys.stderr)
    result = subprocess.run(["sudo", "-v"], check=False)
    return result.returncode == 0


def load_containers(use_sudo: bool, selected: set[str]) -> list[Container]:
    result = run_command(
        docker_command(
            ["ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}"],
            use_sudo=use_sudo,
        )
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker ps failed")

    containers: list[Container] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue

        container_id, image, name, status = line.split("\t", maxsplit=3)
        container = Container(
            container_id=container_id,
            image=image,
            name=name,
            status=status,
        )
        if selected and container.container_id not in selected and container.name not in selected:
            continue
        containers.append(container)

    return containers


def load_user_usage(container: Container, use_sudo: bool) -> list[UserUsage]:
    script = "find / -xdev -printf '%u %b\\n' 2>/dev/null"
    process = subprocess.Popen(
        docker_command(
            ["exec", "-u", "0", container.container_id, "sh", "-c", script],
            use_sudo=use_sudo,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    usage_by_user: dict[str, int] = {}
    with tqdm(
        desc=f"Counting users in {container.name}",
        unit="files",
        file=sys.stdout,
        dynamic_ncols=True,
        leave=True,
    ) as user_progress:
        if process.stdout is not None:
            for line in process.stdout:
                user_progress.update(1)
                try:
                    user, blocks = line.rsplit(maxsplit=1)
                except ValueError:
                    continue

                try:
                    usage_by_user[user] = (
                        usage_by_user.get(user, 0) + int(blocks) * BYTES_IN_BLOCK
                    )
                except ValueError:
                    continue

    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code != 0:
        error = stderr.strip() or "failed to collect user usage"
        return [UserUsage(container.name, "ERROR", 0, error=error)]

    return [
        UserUsage(container.name, user, bytes_used)
        for user, bytes_used in sorted(
            usage_by_user.items(), key=lambda item: item[1], reverse=True
        )
    ]


def print_user_usage_table(usages: list[UserUsage], prev_container: dict[str, Any] | None) -> None:
    table = PrettyTable()
    table.field_names = ["User", "Used (MB/GB/TB)", "Trend", "Notes"]
    table.align = "l"
    table.align["Used (MB/GB/TB)"] = "r"
    table.align["Trend"] = "r"

    prev_users = prev_container.get("users", {}) if prev_container else {}

    for usage in usages:
        prev_bytes = prev_users.get(usage.user)
        trend = format_trend(usage.bytes_used, prev_bytes)
        table.add_row(
            [
                usage.user,
                format_size(usage.bytes_used),
                trend.strip(),
                usage.error or "",
            ]
        )

    container_name = usages[0].container_name if usages else "unknown"
    total_bytes = sum(usage.bytes_used for usage in usages if usage.error is None)
    
    prev_total_bytes = prev_container.get("total_bytes") if prev_container else None
    total_trend = format_trend(total_bytes, prev_total_bytes)

    table.add_row(["TOTAL", format_size(total_bytes), total_trend.strip(), "sum of user file sizes"])
    print(f"\nPer-User Usage in {container_name}")
    print(table)


def write_markdown_report(
    output_path: Path, 
    reports: list[ContainerReport], 
    completed_count: int, 
    total_count: int,
    previous_history: dict[str, Any] | None,
) -> None:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Docker Container User Storage Usage",
        "",
        f"Generated: {generated_at}",
        f"Completed containers: {completed_count}/{total_count}",
        "",
    ]

    grand_total = 0
    prev_containers = previous_history.get("containers", {}) if previous_history else {}

    for report in reports:
        container = report.container
        total_bytes = sum(
            usage.bytes_used for usage in report.usages if usage.error is None
        )
        grand_total += total_bytes

        prev_container = prev_containers.get(container.name)
        prev_total_bytes = prev_container.get("total_bytes") if prev_container else None
        total_trend = format_trend(total_bytes, prev_total_bytes)

        lines.extend(
            [
                f"## {markdown_escape(container.name)}",
                "",
                f"- Container ID: `{markdown_escape(container.container_id)}`",
                f"- Image: `{markdown_escape(container.image)}`",
                f"- Status: {markdown_escape(container.status)}",
                f"- Total container size: {format_size(total_bytes)}{total_trend}",
                "",
                "| User | Used | Trend | Notes |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        
        prev_users = prev_container.get("users", {}) if prev_container else {}
        for usage in report.usages:
            prev_bytes = prev_users.get(usage.user)
            trend = format_trend(usage.bytes_used, prev_bytes)
            lines.append(
                "| "
                f"{markdown_escape(usage.user)} | "
                f"{format_size(usage.bytes_used)} | "
                f"{markdown_escape(trend.strip())} | "
                f"{markdown_escape(usage.error or '')} |"
            )
        lines.extend(
            [
                f"| **TOTAL** | **{format_size(total_bytes)}** | **{markdown_escape(total_trend.strip())}** | sum of user file sizes |",
                "",
            ]
        )

    prev_grand_total = previous_history.get("grand_total_bytes") if previous_history else None
    grand_total_trend = format_trend(grand_total, prev_grand_total)

    lines.extend(
        [
            "## Grand Total",
            "",
            f"Total scanned container size: {format_size(grand_total)}{grand_total_trend}",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    use_sudo = not args.no_sudo
    selected = set(args.container)
    output_path = Path(args.output)
    history_path = Path(args.history)
    
    previous_history = load_previous_history(history_path)

    if not ensure_sudo_authenticated(use_sudo):
        print("Could not authenticate sudo for Docker access.", file=sys.stderr)
        return 1

    try:
        containers = load_containers(use_sudo=use_sudo, selected=selected)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not containers:
        print("No running containers found.")
        return 0

    reports: list[ContainerReport] = []
    for container in tqdm(
        containers,
        desc="Scanning containers",
        unit="container",
        file=sys.stdout,
        dynamic_ncols=True,
    ):
        tqdm.write(f"Running count for {container.name}", file=sys.stdout)
        usages = load_user_usage(container, use_sudo=use_sudo)
        reports.append(ContainerReport(container, usages))
        
        prev_container = previous_history.get("containers", {}).get(container.name) if previous_history else None
        print_user_usage_table(usages, prev_container)
        
        write_markdown_report(output_path, reports, len(reports), len(containers), previous_history)
        tqdm.write(f"Wrote report to {output_path}", file=sys.stdout)

    save_history(history_path, reports)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
