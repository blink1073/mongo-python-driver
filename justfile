# See https://just.systems/man/en/ for instructions

set dotenv-filename := "./.evergreen/scripts/env.sh"

# Make the default recipe private so it doesn't show up in the list.
[private]
default:
  @just --list

[group('docs')]
docs-build:
    hatch run docs:build

[group('docs')]
docs-serve:
    hatch run docs:serve

[group('docs')]
docs-linkcheck:
    hatch run docs:linkcheck

[group('docs')]
docs-test:
    hatch run doctest:test

[group('typing')]
typing:
    hatch run typing:check

[group('typing')]
typing-mypy:
    hatch run typing:mypy

[group('lint')]
lint:
    pre-commit run --all-files

[group('lint')]
lint-manual:
    pre-commit run --all-files --hook-stage manual

[group('test')]
test:
    hatch run test:test

[group('test')]
test-mockupdb:
    hatch run test:test-mockupdb

[group('test')]
test-eg:
    hatch run test:test-eg

[group('encryption')]
setup-encryption:
    bash .evergreen/setup-encryption.sh

[group('encryption')]
teardown-encryption:
    bash .evergreen/teardown-encryption.sh
