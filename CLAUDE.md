# orgface

Do some Emacs org things quickly from the command line.


The main entry is a command line program based on Click:

```
orgface [general-options] <command> [command-options-and-arguments]
```

CLI commands do something and then exit. With some exceptions, every command emits a help message if given neither options nor arguments. An explicit `-h/--help` is accepted by every command and exits ignoring other arguments.


The implementation is well factored and layered. CLI and UI modules contain no data processing logic; that logic lives in separate Python modules. As new functionality is added or reused across commands, it is factored out — follow DRY, not copy-paste.


Tests live in `tests/` and run with `uv run pytest`. Write tests for new functionality. Keep tests focused and independent; avoid mocking internals.


orgface uses `uv` for development.

```
uv sync              # update development area after changes to pyproject.toml
uv run pytest        # run unit tests
uv run orgface       # run CLI
```


orgface follows freedesktop's XDG convention to locate configuration files, hold state, and use file cache. It accepts a `config.toml` on the command line or looks for one at `~/.config/orgface/config.toml`. Configuration is layered in last-one-wins order: environment variables → config file → command line options.
