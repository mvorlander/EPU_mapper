# CryoSPARC–EPU ice comparison

EPU Mapper can join CryoSPARC exposure tables to EPU FoilHole target metadata
without importing the XML into CryoSPARC. The stable key is the
`FoilHole_<ID>` token retained in CryoSPARC's movie or micrograph path.

The two measurements are not equivalent:

- CryoSPARC records its relative estimate in
  `ctf_stats/ice_thickness_rel`.
- Conventional EPU hole filtering records `PixelIntensityMean` in
  `Metadata/GridSquare_<ID>.dm`. This is transmitted intensity: higher values
  mean thinner ice. It is not a calibrated physical thickness when
  `EpuSession.dm` contains `IceThicknessEnabled=false`.

For the correlation plots, EPU intensity is converted to
`ln(session median intensity / intensity)`. This produces a dimensionless
attenuation proxy that increases toward thicker ice, matching the direction
of CryoSPARC relative ice thickness. The raw-intensity correlations are also
retained in the JSON summary for traceability.

Run the comparison with an exposure table that contains both fields, or pass a
second UID-joinable passthrough table when the path and ice estimate are split:

```bash
python scripts/compare_epu_cryosparc_ice.py \
  --epu-session /path/to/EPU-session \
  --exposures /path/to/exposures.cs \
  --passthrough /optional/path/to/passthrough.cs \
  --output-dir /path/to/output
```

Outputs include micrograph-level and FoilHole-median CSV files, unmatched rows,
a JSON provenance/statistics summary, a 300-dpi PNG, and an editable SVG plot.
No movies, micrographs, or particle stacks are read.

## Why Import Beam Shift did not import the ice values

CryoSPARC's **Import Beam Shift** job reads `microscopeData/optics/BeamShift`
from exposure XML files. It does not read EPU FoilHole-filter intensities from
GridSquare metadata. If EPU movie names end in a suffix such as
`_EER_0000000001.eer`, CryoSPARC also needs filename cut settings that reduce
the movie and XML basenames to the same acquisition stem. These beam-shift
values are independent of the EPU intensity comparison described above.
