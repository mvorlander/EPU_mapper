"""Resume copying a generated test fixture to its explicitly marked destination.

Existing files must exactly match the fixture; unrelated data is never replaced.
Parallelism is deliberately limited for shared microscope storage.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path


def copy_fixture(source, destination, workers=4):
    source, destination = Path(source), Path(destination)
    for root in (source, destination):
        marker = json.loads((root / 'SIMULATED_DATA.json').read_text())
        if marker.get('simulated') is not True:
            raise ValueError(f'Not a marked simulated dataset: {root}')
    if (source / 'SIMULATED_DATA.json').read_bytes() != (destination / 'SIMULATED_DATA.json').read_bytes():
        raise ValueError('Source and destination simulation manifests differ')
    paths = sorted(source.rglob('*'))
    for path in paths:
        if path.is_dir():
            (destination / path.relative_to(source)).mkdir(exist_ok=True)
    files = [path for path in paths if path.is_file()]

    def copy(path):
        target = destination / path.relative_to(source)
        payload = path.read_bytes()
        try:
            handle = target.open('xb')
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ValueError(f'Existing file differs; refusing to overwrite: {target}')
            return 'existing'
        try:
            with handle:
                handle.write(payload)
        except BaseException:
            # Only remove this worker's newly created incomplete file.
            target.unlink(missing_ok=True)
            raise
        return 'copied'

    counts = {'existing': 0, 'copied': 0}
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as pool:
        for number, result in enumerate(pool.map(copy, files), 1):
            counts[result] += 1
            if number % 500 == 0:
                print(f'Files complete: {number}/{len(files)} {counts}', flush=True)
    print(json.dumps(dict(destination=str(destination), files=len(files), **counts)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    copy_fixture(args.source, args.destination)
