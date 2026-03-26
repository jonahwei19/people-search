#!/bin/bash
# Sync shared UI to cloud (sets APP_MODE to 'cloud')
sed "s/'{{APP_MODE}}'/'cloud'/" shared/ui.html > cloud/public/index.html
echo "Synced shared/ui.html → cloud/public/index.html"
