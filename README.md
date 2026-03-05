# GitHub Copilot Model Notifier

Get notified when GitHub Copilot's supported AI models change — new models added, models retired, or multipliers updated.
A daily check run executes at 8:00 UTC, and checks the official GitHub docs for changes to the models. If so, it creates a data point that will be reflected as a Release in this repo as well as an entry in the RSS feed in our website. Subscribe to either of those to get notified.

## Subscribe to notifications

You have two options:

### Option 1 — RSS Feed

Point your RSS reader at:
```
https://rajbos.github.io/github-copilot-model-notifier/feed.xml
```

### Option 2 — GitHub Release notifications

1. Click **Watch** (top-right of this page).
2. Select **Custom → Releases**.

You will receive a notification every time a new release is published.

# How it works

A scheduled GitHub Actions workflow runs every day and scrapes the [GitHub Copilot supported models](https://docs.github.com/en/copilot/reference/ai-models/supported-models) documentation page.
When changes are detected (new model, removed model, or updated provider/multiplier), the workflow:

1. Updates [`data/models.json`](data/models.json) with the latest model snapshot.
2. Appends an entry to [`data/changes.json`](data/changes.json) with the diff.
3. Regenerates the [GitHub Pages site](https://rajbos.github.io/github-copilot-model-notifier/) with the latest model table and change history.
4. Creates a **GitHub Release** containing the change summary.

## GitHub Pages setup

GitHub Pages must be enabled once in the repository settings:

1. Go to **Settings → Pages**.
2. Under **Source**, choose **Deploy from a branch**.
3. Select branch `main` and folder `/docs`.
4. Click **Save**.

The pages site will be available at `https://<owner>.github.io/github-copilot-model-notifier/`.

## Running manually

Trigger the workflow at any time via **Actions → Check GitHub Copilot Models → Run workflow**.

## Data files

| File | Description |
| ---- | ----------- |
| [`data/models.json`](data/models.json) | Current model snapshot used for comparison |
| [`data/changes.json`](data/changes.json) | Full history of detected changes |
| [`docs/index.html`](docs/index.html) | Generated GitHub Pages index |
| [`docs/feed.xml`](docs/feed.xml) | Generated RSS feed |
