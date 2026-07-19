from pathlib import Path


def write_capture(directory: Path, name: str = "x.md", title: str = "t") -> None:
    """Write a minimal valid capture card into *directory*."""
    (directory / name).write_text(
        "---\n"
        "harness: claude-code\n"
        "session_id: 11111111-2222-3333-4444-555555555555\n"
        "cwd: /Users/x/proj\n"
        f"title: {title}\n"
        "captured_at: 2026-07-19T12:00:00+08:00\n"
        "---\n\n"
        "capture body\n"
    )
