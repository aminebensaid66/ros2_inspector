from importlib.metadata import PackageNotFoundError, version

from ros2inspector import __version__
from ros2inspector.cli._output import OutputFormat


def test_runtime_version_comes_from_installed_metadata() -> None:
    try:
        installed_version = version("ros2inspector")
    except PackageNotFoundError:
        assert __version__ == "0.0.0+unknown"
    else:
        assert __version__ == installed_version


def test_string_enum_is_python_310_compatible() -> None:
    assert str(OutputFormat.JSON) == "json"
    assert OutputFormat.JSON == "json"
