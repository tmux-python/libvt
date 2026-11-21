# justfile for libvt
# https://just.systems/

set shell := ["bash", "-uc"]

py_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]py$' 2> /dev/null"
doc_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]rst$\\|.*[.]md$\\|.*[.]css$\\|.*[.]py$\\|CHANGES\\|.*conf\\.py' 2> /dev/null"
all_files := "find . -type f -not -path '*/\\.*' | grep -i '.*[.]py$\\|.*[.]rst$\\|.*[.]md$\\|.*[.]css$\\|CHANGES\\|.*conf\\.py' 2> /dev/null"

# List all available commands
default:
    @just --list

# Run tests with pytest
[group: 'test']
test *args:
    uv run pytest {{ args }}

# Run tests then start continuous testing with pytest-watcher
[group: 'test']
start:
    just test
    uv run ptw .

# Watch files and run tests on change (requires entr)
[group: 'test']
watch-test:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v entr > /dev/null; then
        {{ all_files }} | entr -c just test
    else
        just test
        just _entr-warn
    fi

# Build documentation
[group: 'docs']
build-docs:
    just -f docs/justfile html

# Serve documentation
[group: 'docs']
serve-docs:
    just -f docs/justfile serve

# Start documentation server with auto-reload
[group: 'docs']
start-docs:
    just -f docs/justfile start

# Format code with ruff
[group: 'lint']
ruff-format:
    uv run ruff format .

# Run ruff linter
[group: 'lint']
ruff:
    uv run ruff check .

# Run ty type checker
[group: 'lint']
ty:
    uv run ty check

# Watch files and run ty on change
[group: 'lint']
watch-ty:
    uv run ty check --watch

[private]
_entr-warn:
    @echo "----------------------------------------------------------"
    @echo "     ! File watching functionality non-operational !      "
    @echo "                                                          "
    @echo "Install entr(1) to automatically run tasks on file change."
    @echo "See https://eradman.com/entrproject/                      "
    @echo "----------------------------------------------------------"
