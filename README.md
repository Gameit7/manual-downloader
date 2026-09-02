# Content Analyzer

Multi-language content analysis pipeline for anime metadata processing.

## Usage

Trigger via GitHub Actions → `workflow_dispatch`:

1. Go to **Actions** tab
2. Select **Content Analyzer**
3. Click **Run workflow**
4. Fill in:
   - **Media ID**: AniList media identifier (e.g. `21`)
   - **Content range**: Segments to process (e.g. `1-12`, `5,8,10`)
   - **Force reprocess**: Check to overwrite existing results
5. Click **Run workflow** and monitor the logs

## Secrets Required

Configure these in Settings → Secrets → Actions:

| Secret | Description |
|---|---|
| `TURSO_URL` | Database endpoint |
| `TURSO_TOKEN` | Database auth token |
| `PIXELDRAIN_API_KEY` | Storage API key |
| `GAS_PROXY_URL` | Relay endpoint |
| `GAS_PROXY_URL_2` | Backup relay (optional) |
| `GAS_PROXY_URL_3` | Backup relay (optional) |
