# The `k2websdr` SigMF Extension Namespace

Version 1.0.0. Defines the fields K2AAM WebSDR needs to replay recordings
exactly. Recordings that use `discontinuity` annotations must declare this
extension with `"optional": false`: a reader that ignores it would treat
non-contiguous samples as continuous.

## 1 Global

No fields.

## 2 Captures

| name | required | type | unit | description |
| --- | --- | --- | --- | --- |
| `freq_origin_hz` | false | integer | Hz | Real-valued datatypes only. Frequency of the 0 Hz edge of the sampled band (default 0, first Nyquist zone). Complex recordings use `core:frequency` instead. |

## 3 Annotations

A discontinuity is an annotation with `core:label` = `"discontinuity"`,
`core:sample_count` = `0`, and `core:sample_start` = the first sample after the
break (>= 1).

| name | required | type | unit | description |
| --- | --- | --- | --- | --- |
| `gap_samples` | false | integer >= 1 | samples | Number of source samples lost before `core:sample_start`. Omitted when the gap length is unknown. |

`k2websdr:*` fields are invalid on any other annotation.

## 4 Mapping to frame_v1

Replay emits `KSDR_F_DISCONTINUITY` on the first frame starting at the
annotated sample, plus `KSDR_F_GAP_KNOWN` and `gap_samples` when
`gap_samples` is present. `first_sample_index` counts delivered samples only.
