#!/usr/bin/env bash
set -euo pipefail

APPTAINER_IMAGE="${APPTAINER_IMAGE:-/groups/plaschka/shared/software/predictomes/containers/build_ppi_network.sif}"

if [[ ! -f "$APPTAINER_IMAGE" ]]; then
  echo "Cannot find Apptainer image: $APPTAINER_IMAGE" >&2
  exit 1
fi

apptainer exec "$APPTAINER_IMAGE" predictomes-build-ppi-network "$@"
