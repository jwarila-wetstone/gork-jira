#!/bin/sh
# Put gj on PATH. Run `gj install` afterwards to teach your agents about it.
set -e
dir=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
ln -sf "$dir/gj" "$HOME/.local/bin/gj"
echo "linked $HOME/.local/bin/gj -> $dir/gj"
