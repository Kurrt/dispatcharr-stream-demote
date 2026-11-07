# Contributing

Issues and pull requests are welcome. Please describe what Dispatcharr version you run and
what you saw, and leave out stream URLs and credentials.

## Tests

Run the tests from the top folder, with Python 3.11 or newer:

```bash
python3 -m unittest discover -s tests -t .
```

They use a fake clock and a fake Dispatcharr, so they touch nothing real. The README says which
tests need ffmpeg or node.

## Building the zip

```bash
python3 scripts/build_zip.py
```

The zip lands in `dist/`.

## Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
type(scope): summary
```

Use `feat` for a new feature, `fix` for a bug fix, `docs` for documentation only, `test` for
tests only, and `chore` for anything else.
