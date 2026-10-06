"""Plain-text access reports."""


def format_report(rows: list[tuple[str, bool]]) -> str:
    """Render one line per user: `name: allowed` or `name: denied`."""
    lines = [f"{name}: {'allowed' if allowed else 'denied'}" for name, allowed in rows]
    return "\n".join(lines)
