from ros2inspector.utils.enums import StrEnum


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    YAML = "yaml"


def health_bar(score: int, include_score: bool = True) -> str:
    filled = round(score / 10)
    color = "green" if score >= 70 else "yellow" if score >= 40 else "red"
    bar = "█" * filled + "░" * (10 - filled)
    suffix = f" {score}/100" if include_score else ""
    return f"[{color}]{bar}[/{color}]{suffix}"
