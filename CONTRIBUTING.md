# Contributing to Nautobot Agent

Thanks for contributing!

## Branching strategy

We use GitHub Flow. `main` is our primary development branch. All changes must be submitted through pull requests targeting `main`.

1. Check existing issues or open an issue before starting significant work.
2. Fork the repository and branch from the latest `main`.
3. Name your branch `feat/<description>`, `fix/<description>`, `docs/<description>`, or `chore/<description>`.
4. Make focused changes and add or update tests as appropriate.
5. Open a pull request against `main` using our PR template.
6. Address review feedback and ensure required CI checks pass.
7. A maintainer will squash-merge accepted changes and delete the temporary branch.

Do not push directly to `main`. Releases are created by maintainers using version tags such as `v1.0.1`.

For local development, see the Docker development guide linked in the README.
