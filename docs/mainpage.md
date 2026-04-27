# Syncraft Documentation

Syncraft is a standalone Python package and CLI for synchronized messaging workflows. This Doxygen site publishes the package API, schema models, and the main application facade used by local automation.

## What this site includes

- Public Python modules under `src/syncraft`
- Pydantic request and response models
- The `SyncraftApp` facade used by the CLI and import-based integrations
- Source browsing for generated documentation pages

## Local build

Install Doxygen and Graphviz, then run:

```sh
doxygen docs/Doxyfile
```

The generated site is written to:

```text
site/html
```

## GitHub Pages deployment

This repository includes a workflow at `.github/workflows/deploy-docs.yml` that builds the Doxygen site and publishes it to GitHub Pages when changes are pushed to `main`.

Use the Pages settings in GitHub to set:

- Source: `GitHub Actions`

For a project repository named `Syncraft`, the published URL is typically:

```text
https://<your-github-username>.github.io/Syncraft/
```

For a repository named `<your-github-username>.github.io`, the published URL is:

```text
https://<your-github-username>.github.io/
```
