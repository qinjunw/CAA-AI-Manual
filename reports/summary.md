# CAA AI Manual Public Index Summary

## Source Baseline

- Source URI root: `caadoc://`
- Baseline: `CATIA V5R21 CAADoc`
- HTML files indexed by the full local build: `9390`
- TOC XML files indexed by the full local build: `189`
- C++ example files indexed by the full local build: `1199`

## Public Artifact

- Content mode: `index-only`
- Embedded official text: `false`
- SQLite database: `data/manual.sqlite`
- Catalog nodes: `6461`
- API pages: `6616`
- API members: `31229`
- Chinese aliases: `146`
- Evidence source locations: `50179`
- API example source links: `18717`
- Raw entities: `0`
- Raw relations: `0`
- Raw chunks: `0`

## Verification Commands

- `python -m unittest discover -s tests -v`
- `python tools/caa_manual_cli.py status`
- `python tools/caa_manual_cli.py search CATGeoFactory --limit 3`
- `python tools/caa_manual_cli.py get-api CATGeoFactory --include members`
