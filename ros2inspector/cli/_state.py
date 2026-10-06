class _AppState:
    quiet: bool = False
    include_tests: bool = False
    factory_patterns: str | None = None
    preset: str | None = None


state = _AppState()
