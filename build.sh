#!/bin/sh
python -m http.server 8000 &
SERVER_PID=$!
trap "kill $SERVER_PID" EXIT


cd ../normcap
rm ./dist/*.whl
poetry build -f wheel
HASH=$(sha256sum dist/*.whl | sed 's/\s.*//')

cd ../normcap-flathub
rm -rf ./build ./.flatpak-builder
sed -z 's/\(\"http.*normcap-.*whl\",\n.*\"sha256\": \"\)\(\w*\)\(\"\)/\1'$HASH'\3/' -i python3-dependencies.json
flatpak-builder --user --install --disable-cache --force-clean build com.github.dynobo.normcap.yml

