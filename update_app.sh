#!/bin/bash
git add munagrocery.apk index.html
git commit -m "Auto update app: $(date '+%Y-%m-%d %H:%M:%S')"
git push origin $(git branch --show-current)
echo "APK pushed to GitHub. Cloudflare Pages deployment triggered!"