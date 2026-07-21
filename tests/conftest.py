from pathlib import Path


def write_capture(
    directory: Path,
    name: str = "x.md",
    title: str = "t",
    cwd: str = "/Users/x/proj",
    captured_at: str = "2026-07-19T12:00:00+08:00",
) -> None:
    """Write a minimal valid capture card into *directory*."""
    (directory / name).write_text(
        "---\n"
        "harness: claude-code\n"
        "session_id: 11111111-2222-3333-4444-555555555555\n"
        f"cwd: {cwd}\n"
        f"title: {title}\n"
        f"captured_at: {captured_at}\n"
        "---\n\n"
        "capture body\n"
    )
