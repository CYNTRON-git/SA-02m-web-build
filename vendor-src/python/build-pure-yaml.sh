#!/bin/bash
set -euo pipefail
/usr/bin/python3 -m venv /work/build-env
/work/build-env/bin/python -m pip install --no-index --no-deps --find-links /work/input/build-tools setuptools==84.2.1 wheel==0.48.0 packaging==26.4
for run in first second; do
    mkdir -p "/work/$run"
    tar -xzf /work/input/downloads/PyYAML-6.0.1.tar.gz --strip-components=1 -C "/work/$run"
    cd "/work/$run"
    SOURCE_DATE_EPOCH=1672531200 PYYAML_FORCE_LIBYAML=0 /work/build-env/bin/python setup.py --without-libyaml bdist_wheel > "/work/$run-build.log" 2>&1
done
cmp /work/first/dist/*.whl /work/second/dist/*.whl
sha256sum /work/first/dist/*.whl
/usr/bin/python3 -m venv /work/yaml-runtime
/work/yaml-runtime/bin/python -m pip install --no-index --no-deps /work/first/dist/*.whl
/work/yaml-runtime/bin/python -I -c 'import yaml; assert yaml.__version__=="6.0.1"; assert not yaml.__with_libyaml__; assert yaml.safe_load("a: [1, 2]")=={"a":[1,2]}; print(yaml.__file__)'
