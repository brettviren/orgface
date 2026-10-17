#!/bin/bash
# bake in my usual local install method
uv tool install -e '.[orgfs,semantic,service,github]' --with 'HPI@git+https://github.com/brettviren/HPI' --upgrade --force
