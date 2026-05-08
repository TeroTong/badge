Put Enterprise WeCom verification files here.

Example:
- `WW_verify_xxxxx.txt`

The web container serves these files directly at:
- `http://<your-domain>/WW_verify_xxxxx.txt`
- `http://<your-domain>:<port>/WW_verify_xxxxx.txt`

This avoids SPA routing rewriting the verification file to `index.html`.
